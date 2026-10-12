"""Analysis Engine Phase 2 — QualityReport, CleaningRecipe, apply/undo.

Spec done-when: the dirty fixture produces the EXACT expected recipe, and the
recipe replays identically on a second file with the same schema. Plus the
non-negotiables: nothing applies without a proposal (gate in code), imputation
is never proposed and always carries a caveat, undo restores the original.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from sila.services.analysis import cleaning, quality

FIXTURES = Path(__file__).parent / "fixtures" / "analysis"
DIRTY = FIXTURES / "dirty_sales.csv"
WEEK2 = FIXTURES / "dirty_sales_week2.csv"

EXPECTED_RECIPE = [
    {"op": "trim", "column": "region"},
    {
        "op": "consolidate_categories",
        "column": "region",
        "mapping": {"muscat": "Muscat", "salalah": "Salalah"},
    },
    {"op": "coerce_date", "column": "date", "formats": ["%Y-%m-%d", "%d/%m/%Y"]},
    {"op": "coerce_number", "column": "revenue", "unit": "OMR"},
    {"op": "dedupe"},
]


def _ctx() -> Any:
    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext

    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )
    ctx.build_state["uploaded_files"] = [{"name": "dirty_sales.csv", "content": DIRTY.read_text()}]
    return ctx


# ---------------------------------------------------------------------------
# QualityReport — every detector fires on the dirty fixture
# ---------------------------------------------------------------------------


def test_quality_report_on_dirty_fixture() -> None:
    report = quality.quality_report(str(DIRTY))
    assert report["rows"] == 27
    assert report["duplicate_rows"] == 3
    assert report["missing"]["units"] == round(7 / 27, 3)
    assert report["whitespace"] == ["region"]
    coercions = {c["column"]: c for c in report["coercion"]}
    assert coercions["date"]["to"] == "date" and coercions["date"]["mixed_formats"]
    assert coercions["date"]["formats"] == ["%Y-%m-%d", "%d/%m/%Y"]
    assert coercions["revenue"]["to"] == "number" and coercions["revenue"]["unit"] == "OMR"
    variants = {v["column"]: v["clusters"] for v in report["variants"]}
    canon = {c["key"]: c["canonical"] for c in variants["region"]}
    assert canon == {"muscat": "Muscat", "salalah": "Salalah"}
    assert 0 < report["score"] < 100
    # The biggest deduction is named — dates/numbers stored as text.
    assert "column types" in report["deductions"][0]["reason"]


def test_dirty_fixture_produces_the_exact_expected_recipe() -> None:
    report = quality.quality_report(str(DIRTY))
    assert cleaning.propose_recipe(report) == EXPECTED_RECIPE


# ---------------------------------------------------------------------------
# Apply — effects are ground truth; replay is deterministic and idempotent
# ---------------------------------------------------------------------------


def test_apply_recipe_effects_and_idempotence() -> None:
    cleaned, effects = cleaning.apply_recipe_to_text(DIRTY.read_text(), EXPECTED_RECIPE)
    assert effects["rows_before"] == 27 and effects["rows_after"] == 24
    by_desc = {e["describe"]: e["changed"] for e in effects["ops"]}
    assert by_desc["remove exact duplicate rows"] == 3
    assert by_desc["strip stray whitespace in 'region'"] == 3
    # Coercion effect counts values that FAILED to parse — here, none.
    assert by_desc["parse 'date' as real dates (mixed formats)"] == 0
    # The categories really merged — only canonical spellings remain.
    regions = {line.split(",")[1] for line in cleaned.splitlines()[1:]}
    assert regions == {"Muscat", "Salalah", "Sohar"}


def test_recipe_replays_identically_on_week2() -> None:
    # Same schema, next week's data: the SAME recipe applies cleanly…
    first, effects = cleaning.apply_recipe_to_text(WEEK2.read_text(), EXPECTED_RECIPE)
    second, _ = cleaning.apply_recipe_to_text(WEEK2.read_text(), EXPECTED_RECIPE)
    assert first == second  # deterministic, byte for byte
    assert effects["rows_before"] == 10 and effects["rows_after"] == 9
    # …and the cleaned output needs no further cleaning (idempotence).
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        fh.write(first)
    assert cleaning.propose_recipe(quality.quality_report(fh.name)) == []


def test_validate_ops_rejects_unknown_columns_and_ops() -> None:
    errors = cleaning.validate_ops(
        [{"op": "trim", "column": "ghost"}, {"op": "explode"}], ["region"]
    )
    assert any("ghost" in e for e in errors) and any("explode" in e for e in errors)


def test_impute_never_proposed_and_always_caveated() -> None:
    report = quality.quality_report(str(DIRTY))  # units is 26% missing…
    assert not any(op["op"] == "impute" for op in cleaning.propose_recipe(report))
    _, effects = cleaning.apply_recipe_to_text(
        DIRTY.read_text(), [{"op": "impute", "column": "units", "strategy": "median"}]
    )
    assert effects["caveats"] and "units" in effects["caveats"][0]


# ---------------------------------------------------------------------------
# Tools — propose→choose→apply gate, undo, plan invalidation
# ---------------------------------------------------------------------------


async def test_apply_refuses_without_a_proposal() -> None:
    from sila.services.sila.tool_registry import execute_tool

    res = await execute_tool("apply_cleaning", _ctx(), {})
    assert "propose_cleaning first" in res.content and "Nothing was applied" in res.content


async def test_propose_then_apply_selected_ops_only() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    ctx.build_state["analysis_plan"] = {"hash": "x", "approved": True}
    proposed = await execute_tool("propose_cleaning", ctx, {})
    assert "GROUND TRUTH" in proposed.content and "5 fixes proposed" in proposed.content
    assert ctx.build_state["proposed_cleaning"]["ops"] == EXPECTED_RECIPE
    # The user picked only the dedupe (fix 5).
    applied = await execute_tool("apply_cleaning", ctx, {"ops": [5]})
    assert "27 → 24 rows" in applied.content
    assert ctx.build_state["cleaning_recipe"]["dirty_sales.csv"] == [{"op": "dedupe"}]
    assert "analysis_plan" not in ctx.build_state  # builds must re-plan
    # The stash now holds the cleaned text; the original is kept for undo.
    chip = ctx.build_state["uploaded_files"][-1]
    assert chip["content_raw"] == DIRTY.read_text()
    assert len(chip["content"].splitlines()) == 25  # header + 24 rows


async def test_apply_rejects_non_explicit_extra_ops() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    await execute_tool("propose_cleaning", ctx, {})
    res = await execute_tool("apply_cleaning", ctx, {"extra_ops": [{"op": "trim"}]})
    assert "Nothing applied" in res.content


async def test_undo_restores_the_original_bytes() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    await execute_tool("propose_cleaning", ctx, {})
    await execute_tool("apply_cleaning", ctx, {})
    assert ctx.build_state["uploaded_files"][-1]["content"] != DIRTY.read_text()
    res = await execute_tool("undo_cleaning", ctx, {})
    assert "ORIGINAL" in res.content
    assert ctx.build_state["uploaded_files"][-1]["content"] == DIRTY.read_text()
    assert "dirty_sales.csv" not in (ctx.build_state.get("cleaning_recipe") or {})


async def test_plan_proposal_shows_the_cleaning_line() -> None:
    from sila.services.sila.tool_registry import _template_propose_gate, execute_tool

    ctx = _ctx()
    ctx.build_state["active_template"] = "tpl-data-analyst"
    ctx.build_state["turn_seq"] = 1
    await execute_tool("propose_cleaning", ctx, {})
    await execute_tool("apply_cleaning", ctx, {})
    gate = await _template_propose_gate(ctx, ctx.build_state["uploaded_files"][-1])
    assert gate is not None and "Cleaning    : 5 fixes applied" in gate.content


@pytest.mark.parametrize("tool", ["propose_cleaning", "apply_cleaning", "undo_cleaning"])
async def test_cleaning_tools_need_a_data_file(tool: str) -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    ctx.build_state["uploaded_files"] = []
    res = await execute_tool(tool, ctx, {})
    assert "No data file" in res.content
