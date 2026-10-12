"""Oman PDPL policy loader (Phase 15 / 3B).

Reads `infra/policies/pdpl.yaml` — the machine-checkable rules derived from
the PDPL Executive Regulations + AI/Cloud policies — and exposes them typed
for the WorkflowScanner. Findings cite articles via `articles`.

Same loader conventions as classification_policy.py: explicit path for tests,
env override for containers, repo fallback for dev. Cached per process.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


@dataclass(frozen=True)
class PdplPolicy:
    articles: dict[str, str]
    sensitive_data_keywords: tuple[str, ...]
    denied_node_type_substrings: tuple[str, ...]
    external_effect_node_type_substrings: tuple[str, ...]
    secret_patterns: tuple[re.Pattern[str], ...]
    internal_host_substrings: tuple[str, ...]

    def article(self, key: str) -> str:
        """PDPL article reference for a rule group ('' when unmapped)."""
        return self.articles.get(key, "")

    def is_internal_host(self, host: str) -> bool:
        low = host.lower()
        return any(s in low for s in self.internal_host_substrings)


def _repo_policy_path() -> Path:
    """Locate infra/policies/pdpl.yaml from the repo (host/dev fallback).

    Computed lazily — never at import time — so it can't crash in containers
    where the file tree is shallower (the env var is set there anyway).
    """
    here = Path(__file__).resolve()
    for up in (5, 4):  # repo root from .../apps/api/sila/core/policies/
        if up < len(here.parents):
            candidate = here.parents[up] / "infra" / "policies" / "pdpl.yaml"
            if candidate.exists():
                return candidate
    raise FileNotFoundError("pdpl.yaml not found; set SILA_PDPL_POLICY_PATH")


@lru_cache(maxsize=1)
def get_pdpl_policy(path: Path | None = None) -> PdplPolicy:
    """Cached loader. Resolution order: `path` arg (tests) →
    SILA_PDPL_POLICY_PATH env (containers) → repo fallback (host/dev)."""
    if path is not None:
        src = Path(path)
    else:
        env_path = os.environ.get("SILA_PDPL_POLICY_PATH")
        src = Path(env_path) if env_path else _repo_policy_path()
    raw = yaml.safe_load(src.read_text(encoding="utf-8"))

    return PdplPolicy(
        articles={str(k): str(v) for k, v in (raw.get("articles") or {}).items()},
        sensitive_data_keywords=tuple(
            str(k).lower() for k in raw.get("sensitive_data_keywords") or []
        ),
        denied_node_type_substrings=tuple(
            str(k).lower() for k in raw.get("denied_node_type_substrings") or []
        ),
        external_effect_node_type_substrings=tuple(
            str(k).lower() for k in raw.get("external_effect_node_type_substrings") or []
        ),
        secret_patterns=tuple(re.compile(p) for p in raw.get("secret_patterns") or []),
        internal_host_substrings=tuple(
            str(k).lower() for k in raw.get("internal_host_substrings") or []
        ),
    )
