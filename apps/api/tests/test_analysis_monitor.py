"""Analysis Engine Phase 6 — refresh, drift, alert rules.

Spec done-when: re-running on a changed schema reports the drift BEFORE
rebuilding. Plus: the recipe replays on fresh data, alerts evaluate in code,
and the pipeline exports for the user-owned agent copy.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sila.services.analysis import monitor

FIXTURES = Path(__file__).parent / "fixtures" / "analysis"
GOLDEN = FIXTURES / "golden_sales.csv"
DIRTY = FIXTURES / "dirty_sales.csv"
WEEK2 = FIXTURES / "dirty_sales_week2.csv"

# golden_sales.csv with `revenue` renamed → a schema change on "re-upload".
RENAMED = GOLDEN.read_text().replace("revenue", "income")


def _ctx(name: str = "golden_sales.csv", content: str | None = None) -> Any:
    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext

    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )
    ctx.build_state["uploaded_files"] = [
        {"name": name, "content": content if content is not None else GOLDEN.read_text()}
    ]
    return ctx


# ---------------------------------------------------------------------------
# Baseline + drift mechanics
# ---------------------------------------------------------------------------


def test_baseline_and_clean_drift() -> None:
    base = monitor.capture_baseline(str(GOLDEN))
    assert base["rows"] == 8 and base["hash"]
    assert "revenue" in base["metrics"] and base["metrics"]["revenue"]["mean"] == 1262.5
    report = monitor.drift_report(base, monitor.capture_baseline(str(GOLDEN)))
    assert report["clean"] and not report["schema_changed"]


def test_schema_change_and_volume_flag_detected(tmp_path: Path) -> None:
    base = monitor.capture_baseline(str(GOLDEN))
    renamed = tmp_path / "renamed.csv"
    renamed.write_text(RENAMED)
    report = monitor.drift_report(base, monitor.capture_baseline(str(renamed)))
    assert report["schema_changed"]
    assert any("revenue" in c and "disappeared" in c for c in report["changes"])
    assert any("income" in c and "appeared" in c for c in report["changes"])
    # Volume: 27-row dirty file vs the 8-row golden baseline → flag.
    vol = monitor.drift_report(base, monitor.capture_baseline(str(DIRTY)))
    assert vol["volume"]["flag"]


def test_distribution_shift_flagged(tmp_path: Path) -> None:
    shifted = tmp_path / "shifted.csv"
    shifted.write_text(
        "date,region,revenue,units,order_id\n"
        + "\n".join(f"2026-02-0{i},North,{5000 + i},{10 + i},{6000 + i}" for i in range(1, 9))
    )
    base = monitor.capture_baseline(str(GOLDEN))
    report = monitor.drift_report(base, monitor.capture_baseline(str(shifted)))
    assert any(s["column"] == "revenue" for s in report["shifts"])
    assert "SHIFT" in monitor.describe_drift(report)


# ---------------------------------------------------------------------------
# Done-when — drift reported BEFORE rebuilding
# ---------------------------------------------------------------------------


async def test_refresh_blocks_on_schema_drift_until_acknowledged() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    first = await execute_tool("refresh_analysis", ctx, {})
    assert first.ui is not None  # baseline established, dashboard built
    assert "Baseline established" in first.content
    # "Next week": same name, changed schema.
    ctx.build_state["uploaded_files"][-1]["content"] = RENAMED
    blocked = await execute_tool("refresh_analysis", ctx, {})
    assert blocked.ui is None  # NOT rebuilt — the done-when
    assert "DRIFT DETECTED" in blocked.content and "NOT rebuilt" in blocked.content
    assert "revenue" in blocked.content
    # The user says go ahead → acknowledged refresh rebuilds.
    allowed = await execute_tool("refresh_analysis", ctx, {"acknowledge_drift": True})
    assert allowed.ui is not None and "Drift:" in allowed.content


async def test_refresh_keeps_an_approved_plan_approved() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    ctx.build_state["active_template"] = "tpl-data-analyst"
    ctx.build_state["turn_seq"] = 1
    gate1 = await execute_tool("create_data_dashboard", ctx, {})
    assert gate1.ui is None  # plan proposed
    ctx.build_state["turn_seq"] = 2
    built = await execute_tool("create_data_dashboard", ctx, {})
    assert built.ui is not None  # approved + built
    # New rows arrive (same schema) → refresh must NOT re-gate the user.
    ctx.build_state["uploaded_files"][-1]["content"] = (
        GOLDEN.read_text() + "2026-01-09,North,1100,11,5009\n"
    )
    ctx.build_state["turn_seq"] = 3
    refreshed = await execute_tool("refresh_analysis", ctx, {})
    assert refreshed.ui is not None
    assert ctx.build_state["analysis_plan"]["approved"] is True


# ---------------------------------------------------------------------------
# Recipe replay + alerts + export
# ---------------------------------------------------------------------------


async def test_refresh_replays_the_cleaning_recipe_on_fresh_data() -> None:
    from sila.services.sila.tool_registry import execute_tool
    from tests.test_analysis_cleaning import EXPECTED_RECIPE

    ctx = _ctx(name="dirty_sales.csv", content=WEEK2.read_text())
    ctx.build_state["cleaning_recipe"] = {"dirty_sales.csv": EXPECTED_RECIPE}
    res = await execute_tool("refresh_analysis", ctx, {})
    assert "recipe replayed (5 fixes)" in res.content
    chip = ctx.build_state["uploaded_files"][-1]
    assert "content_raw" in chip  # original kept
    assert len(chip["content"].splitlines()) == 10  # header + 9 deduped rows
    assert res.ui is not None and res.ui["dashboard"]["rows"] == 9


async def test_alert_rules_lifecycle_and_evaluation() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    bad = await execute_tool("set_alert_rule", ctx, {"metric": "revenue", "cmp": "??"})
    assert "Invalid rule" in bad.content
    added = await execute_tool(
        "set_alert_rule", ctx, {"metric": "revenue", "agg": "avg", "cmp": "gt", "threshold": 1000}
    )
    assert "avg(revenue) > 1000" in added.content
    res = await execute_tool("refresh_analysis", ctx, {})
    assert "ALERT TRIGGERED: avg(revenue) > 1000 — actual value 1262.5" in res.content
    quiet = await execute_tool(
        "set_alert_rule", ctx, {"metric": "units", "agg": "max", "cmp": "gt", "threshold": 99}
    )
    assert "max(units) > 99" in quiet.content
    res2 = await execute_tool("refresh_analysis", ctx, {})
    assert "alert ok: max(units) > 99" in res2.content
    cleared = await execute_tool("set_alert_rule", ctx, {"action": "clear"})
    assert "cleared" in cleared.content and ctx.build_state["alert_rules"] == []


async def test_broken_alert_rule_is_loud_not_silent() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    await execute_tool("set_alert_rule", ctx, {"metric": "churn", "cmp": "gt", "threshold": 5})
    res = await execute_tool("refresh_analysis", ctx, {})
    assert "ALERT RULE BROKEN" in res.content and "churn" in res.content


async def test_monitor_config_exports_the_inheritable_pipeline() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    ctx.build_state["analysis_objective"] = {"question": "Track revenue", "metrics": ["revenue"]}
    ctx.build_state["cleaning_recipe"] = {"golden_sales.csv": [{"op": "dedupe"}]}
    await execute_tool("set_alert_rule", ctx, {"metric": "revenue", "cmp": "gt", "threshold": 1000})
    await execute_tool("refresh_analysis", ctx, {})
    config = ctx.build_state["monitor_config"]
    assert config["objective"]["question"] == "Track revenue"
    assert config["cleaning_recipe"]["golden_sales.csv"] == [{"op": "dedupe"}]
    assert config["alert_rules"] and config["baseline"]["rows"] == 8
    assert any(c.get("metric") == "revenue" for c in config["charts"])
    assert config["semantic_layer"]
