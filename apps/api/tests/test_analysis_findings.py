"""Analysis Engine Phase 3 — statistical toolkit + guardrails.

Spec done-when: the Simpson fixture emits a contradiction caveat, and the
n=7 fixture refuses to claim significance. Plus: p-value numerics pinned to
known table values, Holm correction, causation caveats that cannot be
stripped, and the forecast refusing without two seasonal cycles.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from sila.services.analysis import analyze, findings, stats, temporal

FIXTURES = Path(__file__).parent / "fixtures" / "analysis"
SIMPSON = str(FIXTURES / "simpson_sales.csv")
TINY = str(FIXTURES / "tiny_n7.csv")
SERIES = str(FIXTURES / "timeseries_cp.csv")
GOLDEN = str(FIXTURES / "golden_sales.csv")


def _ctx(file: str, name: str) -> Any:
    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext

    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )
    ctx.build_state["uploaded_files"] = [{"name": name, "content": Path(file).read_text()}]
    return ctx


# ---------------------------------------------------------------------------
# Numerics — pinned to known table values (no scipy in the stack)
# ---------------------------------------------------------------------------


def test_p_values_match_known_tables() -> None:
    assert stats.t_p_two_sided(2.0, 10) == pytest.approx(0.0734, abs=2e-3)
    assert stats.chi2_sf(3.841, 1) == pytest.approx(0.05, abs=2e-3)
    assert 2 * stats.normal_sf(1.96) == pytest.approx(0.05, abs=1e-3)
    # F(1, df) is t² — the two routes must agree.
    assert stats.f_sf(4.0, 1, 10) == pytest.approx(stats.t_p_two_sided(2.0, 10), abs=1e-6)


def test_holm_is_monotone_and_capped() -> None:
    adjusted = stats.holm([0.01, 0.04, 0.03, 0.9])
    assert adjusted[0] == pytest.approx(0.04)  # 4 × 0.01
    assert all(a <= 1.0 for a in adjusted)
    assert adjusted[3] == pytest.approx(0.9)


def test_welch_and_anova_basics() -> None:
    t, df, p = stats.welch_t(50, 10.0, 4.0, 50, 12.0, 4.0)
    assert t < 0 and p < 0.001 and 90 < df <= 98
    f, _, _, p_f = stats.anova_oneway([(30, 10.0, 2.0), (30, 12.0, 2.0), (30, 14.0, 2.0)])
    assert f > 10 and p_f < 1e-6


# ---------------------------------------------------------------------------
# Guardrails — the two done-when tests
# ---------------------------------------------------------------------------


def test_simpson_fixture_emits_contradiction_caveat() -> None:
    found = analyze.compare_groups(SIMPSON, "revenue", "channel", segment="tier")
    headline = found[0]
    assert headline.kind == "difference" and headline.statement_slots["group_high"] == "A"
    assert any("Simpson" in c and "REVERSES" in c for c in headline.caveats)


def test_n7_fixture_refuses_to_claim_significance() -> None:
    found = analyze.compare_groups(TINY, "value", "group")
    finding = found[0]
    assert finding.n == 7 and finding.confidence == "low"
    assert "significant" not in finding.phrasing.lower()
    assert any("too small" in c for c in finding.caveats)


def test_significant_is_unreachable_for_low_confidence() -> None:
    # Even a tiny p cannot put the word into a small-n finding's phrasing.
    assert findings.confidence_for(7, 0.0001) == "low"
    assert findings.significance_note("low", 0.0001, 1) == ""
    assert "significant" in findings.significance_note("high", 0.001, 3)


def test_correlation_carries_the_causation_caveat() -> None:
    found = analyze.correlations(GOLDEN)
    assert found and all(findings.CAUSATION_CAVEAT in f.caveats for f in found)
    # revenue and units co-move in the golden fixture.
    top = found[0]
    assert {top.statement_slots["x"], top.statement_slots["y"]} <= {"revenue", "units", "order_id"}


# ---------------------------------------------------------------------------
# Temporal — changepoint, anomaly, forecast refusal
# ---------------------------------------------------------------------------


def test_changepoint_and_anomaly_detected() -> None:
    found = temporal.trend_findings(SERIES, "value", "date")
    kinds = [f.kind for f in found]
    assert "anomaly" in kinds
    shift = next(f for f in found if "changepoint_date" in f.statement_slots)
    # The level shift is at day 31 (2026-01-31); binary segmentation lands ±2 days.
    assert shift.statement_slots["changepoint_date"][:9] == "2026-01-3"
    anomaly = next(f for f in found if f.kind == "anomaly")
    assert any(a["date"] == "2026-02-15" for a in anomaly.statement_slots["anomalies"])


def test_forecast_refuses_short_history_and_works_on_long() -> None:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        fh.write("date,value\n" + "\n".join(f"2026-01-0{i},{100 + i}" for i in range(1, 6)))
    with pytest.raises(ValueError, match="two full cycles"):
        temporal.forecast_finding(fh.name, "value", "date")
    ok = temporal.forecast_finding(SERIES, "value", "date")
    assert len(ok.statement_slots["forecast"]) == 7
    assert ok.statement_slots["interval_plus_minus"] >= 0


# ---------------------------------------------------------------------------
# Drivers / segments / concentration — sane, typed, caveated
# ---------------------------------------------------------------------------


def test_drivers_ranks_the_true_driver_and_keeps_the_caveat() -> None:
    found = analyze.drivers(SIMPSON, "revenue")
    ranked = found[0].statement_slots["ranked_drivers"]
    assert ranked[0]["column"] == "tier"  # tier determines revenue by construction
    assert findings.CAUSATION_CAVEAT in found[0].caveats


def test_segments_returns_human_readable_rules() -> None:
    found = analyze.segments(SIMPSON, "revenue")
    slots = found[0].statement_slots
    assert slots["segment_column"] == "tier" and slots["variance_explained_pct"] > 80
    assert any("tier=" in r and "× overall revenue" in r for r in slots["rules"])


def test_concentration_shares_and_hhi() -> None:
    found = analyze.concentration(GOLDEN, "revenue", "region")
    slots = found[0].statement_slots
    assert slots["top_group"] and 0 < slots["hhi"] <= 1
    assert slots["top5_share_pct"] == 100.0  # only 4 regions in the golden file


# ---------------------------------------------------------------------------
# The tool — ground-truth block, findings stored, refusals relayed
# ---------------------------------------------------------------------------


async def test_tool_returns_ground_truth_and_stores_findings() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx(SIMPSON, "simpson_sales.csv")
    res = await execute_tool(
        "analyze_data", ctx, {"analysis": "compare", "metric": "revenue", "by": "channel"}
    )
    assert "GROUND TRUTH FINDINGS" in res.content
    assert "CAVEAT (must relay)" in res.content and "Simpson" in res.content
    assert ctx.build_state["findings"] and ctx.build_state["findings"][0]["kind"] == "difference"


async def test_tool_relays_forecast_refusal() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx(TINY, "tiny.csv")
    res = await execute_tool("analyze_data", ctx, {"analysis": "forecast", "date": "date"})
    assert "Couldn't run forecast" in res.content


async def test_tool_rejects_unknown_analysis() -> None:
    from sila.services.sila.tool_registry import execute_tool

    res = await execute_tool("analyze_data", _ctx(TINY, "t.csv"), {"analysis": "hack"})
    assert "must be one of" in res.content


def test_compare_phrasing_uses_plain_language_not_jargon() -> None:
    """Live feedback: 'rank-biserial=-1.00' read as noise. The prose carries a
    plain magnitude label; the raw effect value stays in the typed fields."""
    found = analyze.compare_groups(SIMPSON, "revenue", "channel")
    headline = found[0]
    assert "rank-biserial=" not in headline.phrasing and "Cohen's d=" not in headline.phrasing
    assert " gap, " in headline.phrasing  # "a very large gap, Mann-Whitney test"
    assert headline.effect_size != 0  # the number still travels, typed
