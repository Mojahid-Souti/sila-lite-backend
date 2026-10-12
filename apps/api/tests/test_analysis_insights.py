"""Analysis Engine Phase 4 — rank_insights, narration guard, exec summary.

Spec done-when: a mutation test proving the model cannot emit a number absent
from statement_slots. Here the guard is structural: prose is validated at
construction, so a template mutated to contain a rogue number makes
rank_insights RAISE instead of shipping the text.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from sila.services.analysis import analyze, insights

FIXTURES = Path(__file__).parent / "fixtures" / "analysis"
SIMPSON = str(FIXTURES / "simpson_sales.csv")


def _finding(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "f-difference-1",
        "kind": "difference",
        "statement_slots": {
            "metric": "revenue",
            "by": "region",
            "group_high": "Muscat",
            "mean_high": 1262.5,
            "group_low": "Sohar",
            "mean_low": 795.0,
            "relative_difference_pct": 58.8,
            "n": 24,
        },
        "effect_size": 1.2,
        "p_value": 0.001,
        "n": 24,
        "confidence": "medium",
        "caveats": [],
        "evidence": None,
        "objective_relevance": 1.0,
        "phrasing": "Muscat averages 1262.5 revenue vs Sohar's 795 — 58.8% higher",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# The number guard + the done-when mutation test
# ---------------------------------------------------------------------------


def test_guard_accepts_slot_numbers_and_roundings() -> None:
    slots = {"a": 1234.0, "b": 91.111, "n": 24}
    insights.check_numbers_from_slots("grew to 1,234 (91.1 avg over 24 rows)", slots)


def test_guard_rejects_numbers_absent_from_slots() -> None:
    with pytest.raises(ValueError, match="absent"):
        insights.check_numbers_from_slots("revenue grew 13%", {"a": 12.5})


def test_mutation_rogue_number_in_template_cannot_ship(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE done-when test: mutate the so-what library to inject a number that
    is not in the slots — construction must raise, the text never ships."""
    mutated = dict(insights.SO_WHAT)
    mutated["difference"] = "Closing the gap is worth exactly 42 million to {group_high}."
    monkeypatch.setattr(insights, "SO_WHAT", mutated)
    with pytest.raises(ValueError, match="42"):
        insights.rank_insights([_finding()])


def test_unmutated_templates_pass_the_guard() -> None:
    cards = insights.rank_insights([_finding()])
    assert cards and "Muscat" in cards[0].so_what
    assert cards[0].what_happened.startswith("Muscat averages")


# ---------------------------------------------------------------------------
# Scoring — the spec's product formula
# ---------------------------------------------------------------------------


def test_confident_relevant_findings_outrank_weak_ones() -> None:
    strong = _finding()
    weak = _finding(id="f-2", confidence="low", effect_size=0.1, objective_relevance=0.5, n=7)
    cards = insights.rank_insights([weak, strong])
    assert cards[0].finding_id == "f-difference-1"
    assert cards[0].score > cards[1].score


def test_novelty_decays_repeated_findings() -> None:
    scored = insights.score_findings([_finding(), _finding(id="f-2")])
    assert scored[0][0] > scored[1][0]  # identical finding, second is decayed
    assert scored[1][0] == pytest.approx(scored[0][0] * 0.6)


# ---------------------------------------------------------------------------
# Executive summary
# ---------------------------------------------------------------------------


def test_executive_summary_headline_and_bullets() -> None:
    cards = insights.rank_insights([_finding()])
    summary = insights.executive_summary(cards, {"question": "Which regions drag revenue?"})
    assert summary["objective"] == "Which regions drag revenue?"
    assert 1 <= len(summary["bullets"]) <= 5
    assert summary["headline"]["value"] == 58.8  # relative_difference_pct


def test_summary_over_real_findings_preserves_caveats() -> None:
    found = [f.to_dict() for f in analyze.compare_groups(SIMPSON, "revenue", "channel", "tier")]
    cards = insights.rank_insights(found)
    assert any(any("Simpson" in c for c in card.caveats) for card in cards)


def test_real_pipeline_end_to_end_passes_the_guard() -> None:
    """Real phrasings carry effect sizes, '95% CI', 'cover 80%' — the guard
    basis must accept them while still rejecting anything else."""
    from sila.services.analysis import temporal

    series = str(FIXTURES / "timeseries_cp.csv")
    found = [f.to_dict() for f in temporal.trend_findings(series, "value", "date")]
    found += [f.to_dict() for f in analyze.concentration(SIMPSON, "revenue", "tier")]
    cards = insights.rank_insights(found)  # would raise on any rogue number
    summary = insights.executive_summary(cards)
    assert summary["bullets"] and summary["headline"] is not None


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def _ctx() -> Any:
    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext

    return ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )


async def test_tool_requires_findings_first() -> None:
    from sila.services.sila.tool_registry import execute_tool

    res = await execute_tool("rank_insights", _ctx(), {})
    assert "run analyze_data" in res.content


async def test_tool_ranks_and_stores() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    ctx.build_state["findings"] = [_finding(), _finding(id="f-2", confidence="low", n=7)]
    res = await execute_tool("rank_insights", ctx, {"top_k": 2})
    assert "EXECUTIVE SUMMARY" in res.content and "So what:" in res.content
    assert len(ctx.build_state["insights"]) == 2
    assert ctx.build_state["exec_summary"]["bullets"]


async def test_exec_summary_pins_to_the_chat_dashboard() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    ctx.build_state["uploaded_files"] = [
        {"name": "sales.csv", "content": "region,revenue\nA,10\nB,20\nA,30\n"}
    ]
    ctx.build_state["exec_summary"] = {"bullets": ["Revenue is concentrated in B."]}
    res = await execute_tool("create_data_dashboard", ctx, {})
    assert res.ui is not None
    assert res.ui["dashboard"]["notes"][0] == "Revenue is concentrated in B."


def test_so_what_falls_back_when_slots_are_missing() -> None:
    """Live bug: an ANOVA difference has no group pair, and the template
    rendered 'the gap between … and …'. Missing slots → slot-free fallback."""
    anova = _finding(
        statement_slots={"metric": "revenue", "by": "region", "F": 56.9, "groups": 3, "n": 24},
        phrasing="revenue differs across the 3 region groups (ANOVA F=56.9)",
    )
    cards = insights.rank_insights([anova])
    assert "…" not in cards[0].so_what
    assert cards[0].so_what == insights.SO_WHAT_FALLBACK["difference"]
