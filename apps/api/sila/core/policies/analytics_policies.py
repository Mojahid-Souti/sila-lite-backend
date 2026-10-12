"""Loaders for Phase 8 policy YAMLs (hours_saved, anomalies).

Both files live alongside `classification.yaml` in `infra/policies/`. Same
SILA_*_POLICY_PATH env vars used for deployed containers.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml


def _find_repo_policies_dir() -> Path:
    """Walk up from this file until we find an `infra/policies/` directory.

    Works in both layouts: host repo (apps/api/sila/...) and slim container
    (/app/sila/...). Falls back to a path that the resolver can detect as
    missing.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "infra" / "policies"
        if candidate.is_dir():
            return candidate
    # Sentinel — the resolver layer handles missing-file gracefully via env vars.
    return here.parent / "policies"


REPO_POLICIES_DIR: Final[Path] = _find_repo_policies_dir()


# ---------------------------------------------------------------------------
# Hours-saved
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PatternRule:
    pattern: str
    minutes: int


@dataclass(frozen=True)
class HoursSavedPolicy:
    default_minutes: int
    by_pattern: tuple[_PatternRule, ...]
    by_agent_id: dict[str, int]

    def minutes_for(self, *, agent_id: str, agent_name: str) -> int:
        """First match wins: agent_id → name pattern → default."""
        if agent_id in self.by_agent_id:
            return self.by_agent_id[agent_id]
        for rule in self.by_pattern:
            if fnmatch.fnmatch(agent_name, rule.pattern):
                return rule.minutes
        return self.default_minutes


@lru_cache(maxsize=1)
def get_hours_saved_policy(path: Path | None = None) -> HoursSavedPolicy:
    src = _resolve_path(
        path, env_var="SILA_HOURS_SAVED_POLICY_PATH", default_name="hours_saved.yaml"
    )
    raw = yaml.safe_load(src.read_text(encoding="utf-8"))
    defaults = (raw or {}).get("defaults") or {}
    by_pattern_raw = (raw or {}).get("by_agent_name_pattern") or []
    by_id_raw = (raw or {}).get("by_agent_id") or {}
    return HoursSavedPolicy(
        default_minutes=int(defaults.get("per_invocation_minutes", 3)),
        by_pattern=tuple(
            _PatternRule(
                pattern=str(r.get("pattern", "*")),
                minutes=int(r.get("per_invocation_minutes", 0)),
            )
            for r in by_pattern_raw
        ),
        by_agent_id={k: int(v) for k, v in by_id_raw.items()},
    )


# ---------------------------------------------------------------------------
# Compute cost (local inference)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComputeCostPolicy:
    """Inputs for costing local (Ollama) inference: amortized hardware + energy.

    Power is carried as MEASURED STATES rather than one blended number, because
    the GPU draws very different power while prefilling (peak), decoding
    (sustained), and sitting resident-but-idle under OLLAMA_KEEP_ALIVE. The
    electricity rate is a selectable Omani tariff band in baisa/kWh, converted
    to USD through the CBO peg. All values config-driven (Rule 5).
    """

    hardware_capital_usd: float
    hardware_useful_life_hours: float
    ai_allocation_fraction: float  # share of hardware amortization charged to AI
    # Measured GPU power states (watts).
    cold_idle_watts: float
    warm_idle_watts: float
    decode_watts: float
    prefill_peak_watts: float
    system_overhead_watts: float
    # Electricity: an Omani tariff band (baisa/kWh) + the CBO peg.
    tariff_band: str
    tariff_baisa_per_kwh: float
    omr_per_usd: float
    report_in: str
    cloud_reference_usd_per_1m: float  # what the same tokens would cost in the cloud
    cloud_reference_input_usd_per_1m: float
    cloud_reference_output_usd_per_1m: float
    cloud_reference_name: str

    @property
    def hardware_usd_per_hour(self) -> float:
        if self.hardware_useful_life_hours <= 0:
            return 0.0
        return self.hardware_capital_usd / self.hardware_useful_life_hours

    @property
    def electricity_usd_per_kwh(self) -> float:
        """baisa/kWh -> OMR/kWh -> USD/kWh via the configured peg."""
        if self.omr_per_usd <= 0:
            return 0.0
        return (self.tariff_baisa_per_kwh / 1000.0) / self.omr_per_usd

    def usd_to_report_currency(self, usd: float) -> float:
        """Convert a USD figure into the configured reporting currency."""
        return usd * self.omr_per_usd if self.report_in.upper() == "OMR" else usd

    # Power draw (kW) per state, each including the rest-of-system overhead.
    @property
    def decode_power_kw(self) -> float:
        return (self.decode_watts + self.system_overhead_watts) / 1000.0

    @property
    def prefill_power_kw(self) -> float:
        return (self.prefill_peak_watts + self.system_overhead_watts) / 1000.0

    @property
    def warm_idle_power_kw(self) -> float:
        return (self.warm_idle_watts + self.system_overhead_watts) / 1000.0

    @property
    def power_kw(self) -> float:
        """Back-compat alias — the decode state is the right default for
        'GPU busy' seconds now that prefill/decode are billed separately."""
        return self.decode_power_kw


@lru_cache(maxsize=1)
def get_compute_cost_policy(path: Path | None = None) -> ComputeCostPolicy:
    src = _resolve_path(
        path, env_var="SILA_COMPUTE_COST_POLICY_PATH", default_name="compute_cost.yaml"
    )
    raw = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    hw = raw.get("hardware") or {}
    en = raw.get("energy") or {}
    st = en.get("states") or {}
    el = raw.get("electricity") or {}
    cur = raw.get("currency") or {}
    cl = raw.get("cloud_reference") or {}
    bands = el.get("bands") or {}
    band = str(el.get("band", "commercial"))
    # Unknown band name -> fall back to the commercial rate rather than 0, so a
    # typo can't silently zero out the energy cost.
    baisa = float(bands.get(band, bands.get("commercial", 25)))
    blended = float(cl.get("usd_per_1m_tokens", 0.60))
    return ComputeCostPolicy(
        hardware_capital_usd=float(hw.get("capital_usd", 3000)),
        hardware_useful_life_hours=float(hw.get("useful_life_hours", 26280)),
        ai_allocation_fraction=float(hw.get("ai_allocation_fraction", 1.0)),
        cold_idle_watts=float(st.get("cold_idle_watts", 4)),
        warm_idle_watts=float(st.get("warm_idle_watts", 14)),
        decode_watts=float(st.get("decode_watts", 56)),
        prefill_peak_watts=float(st.get("prefill_peak_watts", 82)),
        system_overhead_watts=float(en.get("system_overhead_watts", 120)),
        tariff_band=band,
        tariff_baisa_per_kwh=baisa,
        omr_per_usd=float(cur.get("omr_per_usd", 0.384497)),
        report_in=str(cur.get("report_in", "USD")),
        cloud_reference_usd_per_1m=blended,
        cloud_reference_input_usd_per_1m=float(cl.get("usd_per_1m_input_tokens", blended)),
        cloud_reference_output_usd_per_1m=float(cl.get("usd_per_1m_output_tokens", blended)),
        cloud_reference_name=str(cl.get("name", "cloud equivalent")),
    )


# ---------------------------------------------------------------------------
# Anomaly rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnomalyRule:
    id: str
    metric: str
    kind: str  # 'zscore' | 'absolute'
    threshold: float
    baseline_days: int  # for zscore only
    min_sample_size: int
    severity: str  # info | low | medium | high | critical
    note: str


@dataclass(frozen=True)
class AnomalyPolicy:
    rules: tuple[AnomalyRule, ...]


@lru_cache(maxsize=1)
def get_anomaly_policy(path: Path | None = None) -> AnomalyPolicy:
    src = _resolve_path(path, env_var="SILA_ANOMALIES_POLICY_PATH", default_name="anomalies.yaml")
    raw = yaml.safe_load(src.read_text(encoding="utf-8"))
    return AnomalyPolicy(
        rules=tuple(
            AnomalyRule(
                id=str(r.get("id", "")),
                metric=str(r.get("metric", "")),
                kind=str(r.get("kind", "absolute")),
                threshold=float(r.get("threshold", 0.0)),
                baseline_days=int(r.get("baseline_days", 7)),
                min_sample_size=int(r.get("min_sample_size", 5)),
                severity=str(r.get("severity", "medium")),
                note=str(r.get("note", "")),
            )
            for r in (raw or {}).get("rules") or []
        )
    )


def _resolve_path(path: Path | None, *, env_var: str, default_name: str) -> Path:
    if path is not None:
        return Path(path)
    env_path = os.environ.get(env_var)
    if env_path:
        return Path(env_path)
    # Containers mount /policies → /policies/<default_name>; host → REPO_POLICIES_DIR/<default_name>
    container_path = Path("/policies") / default_name
    if container_path.exists():
        return container_path
    return REPO_POLICIES_DIR / default_name
