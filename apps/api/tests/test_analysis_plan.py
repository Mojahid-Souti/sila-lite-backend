"""Analysis Engine Phase 1 — the AnalysisPlan gate and set_objective.

Spec done-when: "just build me a dashboard" still works in two turns, and NO
build can run without an approved plan (enforced in code, not persona).
"""

from __future__ import annotations

import uuid
from typing import Any

from sila.services.analysis import plan as plan_mod

_CSV = "date,region,revenue\n2026-01-01,North,100\n2026-01-02,South,60\n"


def _ctx(template: bool = True) -> Any:
    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext

    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )
    if template:
        ctx.build_state["active_template"] = "tpl-data-analyst"
    ctx.build_state["turn_seq"] = 1
    ctx.build_state["uploaded_files"] = [{"name": "sales.csv", "content": _CSV}]
    return ctx


def _user_turn(ctx: Any) -> None:
    ctx.build_state["turn_seq"] = int(ctx.build_state["turn_seq"]) + 1


# ---------------------------------------------------------------------------
# Refusal — no build runs without an approved plan (end-to-end via the tool)
# ---------------------------------------------------------------------------


async def test_build_refuses_without_approved_plan_then_two_turn_happy_path() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    first = await execute_tool("create_data_dashboard", ctx, {})
    assert first.ui is None and "ANALYSIS PLAN" in first.content  # refused: proposal only
    plan = ctx.build_state["analysis_plan"]
    assert plan["approved"] is False and plan["hash"]
    # Turn 2 — the user said yes; the same call now builds.
    _user_turn(ctx)
    second = await execute_tool("create_data_dashboard", ctx, {})
    assert second.ui is not None and second.ui["command"] == "open_dashboard"
    assert ctx.build_state["analysis_plan"]["approved"] is True


async def test_model_cannot_approve_its_own_plan_same_turn() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    await execute_tool("create_data_dashboard", ctx, {})
    retry = await execute_tool("create_data_dashboard", ctx, {})  # same turn_seq
    assert retry.ui is None and "WAIT" in retry.content.upper()
    assert ctx.build_state["analysis_plan"]["approved"] is False


async def test_approved_plan_covers_other_deliverables_and_edits() -> None:
    from sila.services.sila.tool_registry import _template_propose_gate

    ctx = _ctx()
    chosen = {"name": "sales.csv", "content": _CSV}
    assert await _template_propose_gate(ctx, chosen) is not None
    _user_turn(ctx)
    assert await _template_propose_gate(ctx, chosen) is None  # approved
    # Grafana / Power BI on the same approved data pass without a new gate.
    assert await _template_propose_gate(ctx, chosen, deliverable="Grafana dashboard") is None


async def test_reupload_with_different_rows_invalidates_the_hash() -> None:
    from sila.services.sila.tool_registry import _template_propose_gate

    ctx = _ctx()
    chosen = {"name": "sales.csv", "content": _CSV}
    await _template_propose_gate(ctx, chosen)
    _user_turn(ctx)
    assert await _template_propose_gate(ctx, chosen) is None  # approved
    # Same name, new content (an extra row) → identity changed → re-propose.
    grown = {"name": "sales.csv", "content": _CSV + "2026-01-03,East,80\n"}
    _user_turn(ctx)
    regate = await _template_propose_gate(ctx, grown)
    assert regate is not None and "ANALYSIS PLAN" in regate.content


async def test_non_template_sessions_never_gate() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx(template=False)
    res = await execute_tool("create_data_dashboard", ctx, {})
    assert res.ui is not None  # plain chat keeps the v1 direct build


# ---------------------------------------------------------------------------
# set_objective — guided mode; changing the objective re-plans
# ---------------------------------------------------------------------------


async def test_set_objective_stores_ground_truth_and_invalidates_plan() -> None:
    from sila.services.sila.tool_registry import _template_propose_gate, execute_tool

    ctx = _ctx()
    chosen = {"name": "sales.csv", "content": _CSV}
    await _template_propose_gate(ctx, chosen)  # a plan is now held
    res = await execute_tool(
        "set_objective",
        ctx,
        {"question": "Which regions drag Q2 revenue?", "audience": "exec"},
    )
    assert "GROUND TRUTH" in res.content and "Which regions drag Q2 revenue?" in res.content
    assert "analysis_plan" not in ctx.build_state  # held plan invalidated
    # The next proposal carries the user's objective, not the inferred one.
    _user_turn(ctx)
    regate = await _template_propose_gate(ctx, chosen)
    assert regate is not None and "Which regions drag Q2 revenue?" in regate.content


async def test_set_objective_requires_a_question() -> None:
    from sila.services.sila.tool_registry import execute_tool

    res = await execute_tool("set_objective", _ctx(), {"question": "  "})
    assert "No objective" in res.content


def test_normalize_objective_caps_and_enum() -> None:
    obj = plan_mod.normalize_objective(
        {"question": "x" * 999, "audience": "hacker", "metrics": ["a", "", "b"] * 5}
    )
    assert len(obj["question"]) == 300
    assert obj["audience"] == "exec"  # unknown value falls back to the enum default
    assert len(obj["metrics"]) == 6


def test_infer_objective_reads_the_profile() -> None:
    preview = {
        "source": "sales.csv",
        "columns": [
            {"name": "date", "kind": "date"},
            {"name": "region", "kind": "category"},
            {"name": "revenue", "kind": "number"},
        ],
    }
    obj = plan_mod.infer_objective(preview)
    assert obj["mode"] == "auto" and "revenue" in obj["question"]
    assert "region" in obj["question"] and obj["time_grain"] == "day"


def test_plan_hash_binds_objective_file_and_charts() -> None:
    obj = {"question": "q"}
    base = plan_mod.plan_hash(obj, "a.csv", 10, ["t1", "t2"])
    assert plan_mod.plan_hash(obj, "a.csv", 11, ["t1", "t2"]) != base  # rows
    assert plan_mod.plan_hash({"question": "other"}, "a.csv", 10, ["t1", "t2"]) != base
    assert plan_mod.plan_hash(obj, "b.csv", 10, ["t1", "t2"]) != base
    assert plan_mod.plan_hash(obj, "a.csv", 10, ["t2", "t1"]) == base  # order-free
