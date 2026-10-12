"""The plan ticks itself, every step is explained, and each security check says
what it found.

One recorded run sat at 0/6 with two steps visibly finished, went from a tool
result straight into the next tool with no words between, and showed the
gatekeeper as four rows reading "clear". A panel cannot invest in "clear".
"""

from dataclasses import dataclass, field
from typing import Any

from sila.services.sila.orchestrator.graph import (
    _after_tool,
    _auto_tick,
    _plan_frame,
    _scan_result_text,
)


@dataclass
class _Result:
    content: str = "the tool's own words"


@dataclass
class _Finding:
    finding_type: str
    severity: str
    matched_preview: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _plan() -> list[dict[str, Any]]:
    return [
        {"title": "Read the last 7 days of Outlook and pull the escalations", "done": False},
        {"title": "Search once for Omantel's latest published quarterly revenue", "done": False},
        {"title": "Render a dashboard here in the interface", "done": False},
        {"title": "Push the same dashboard to Grafana", "done": False},
        {"title": "Share the weekly pack as a 6-slide deck", "done": False},
        {"title": "Ask the day and time, then build the Retention Pack agent", "done": False},
    ]


# --- the tick is earned by the tool finishing --------------------------------


def test_a_finished_tool_ticks_the_row_it_fulfils() -> None:
    state = {"tasks": _plan()}
    assert _auto_tick("read_outlook_emails", state) is not None
    assert state["tasks"][0]["done"] is True
    assert not any(t["done"] for t in state["tasks"][1:])


def test_rows_tick_in_plan_order_one_per_completion() -> None:
    """Two rows mention a dashboard. The first unfinished match ticks, so the
    in-chat render and the Grafana push tick in sequence, not both at once."""
    state = {"tasks": _plan()}
    _auto_tick("create_data_dashboard", state)
    assert [t["done"] for t in state["tasks"]] == [False, False, True, False, False, False]
    _auto_tick("create_grafana_dashboard", state)
    assert [t["done"] for t in state["tasks"]] == [False, False, True, True, False, False]


def test_a_tool_with_no_matching_row_ticks_nothing() -> None:
    state = {"tasks": _plan()}
    assert _auto_tick("navigate", state) is None
    assert not any(t["done"] for t in state["tasks"])


def test_no_plan_means_no_tick() -> None:
    assert _auto_tick("web_search", {}) is None


# --- every step is explained before the next -----------------------------------


def test_after_a_tool_the_model_is_told_to_explain_then_continue() -> None:
    frames: list[dict[str, Any]] = []
    state = {"tasks": _plan()}
    text = _after_tool("read_outlook_emails", _Result(), state, frames.append)
    assert text.startswith("the tool's own words")
    assert "at least three full sentences" in text
    assert "Still to do:" in text
    assert "Search once for Omantel" in text
    # The tick reached the screen as a plan frame, not just the state.
    assert frames and frames[0]["name"] == "track_tasks"
    assert frames[0]["result"] == "1/6 done"
    assert frames[0]["tasks"][0]["done"] is True


def test_track_tasks_itself_is_never_nudged() -> None:
    """Nudging the plan tool to call the plan tool would loop."""
    frames: list[dict[str, Any]] = []
    text = _after_tool("track_tasks", _Result("Plan updated"), {"tasks": _plan()}, frames.append)
    assert text == "Plan updated"
    assert frames == []


def test_when_every_row_is_done_the_closing_message_is_demanded() -> None:
    state = {"tasks": [{**t, "done": True} for t in _plan()]}
    state["tasks"][-1]["done"] = False
    frames: list[dict[str, Any]] = []
    text = _after_tool("create_agent_workflow", _Result(), state, frames.append)
    assert "at least four sentences" in text
    assert "name every card" in text
    assert "Still to do" not in text


def test_without_a_plan_the_result_passes_through_untouched() -> None:
    text = _after_tool("web_search", _Result("hits"), {}, lambda f: None)
    assert text == "hits"


def test_plan_frame_counts_done_rows() -> None:
    tasks = _plan()
    tasks[0]["done"] = tasks[1]["done"] = True
    frame = _plan_frame(tasks)
    assert frame["result"] == "2/6 done" and frame["name"] == "track_tasks"


# --- a security check says what it checked and what it found ---------------------


def test_a_clear_check_says_what_it_looked_for() -> None:
    text = _scan_result_text("pii", [])
    assert text.startswith("clear")
    assert "national IDs" in text and "mobile numbers" in text


def test_a_finding_is_named_with_its_severity_and_redacted_preview() -> None:
    text = _scan_result_text(
        "pii", [_Finding("pii.oman_national_id", "critical", matched_preview="12****78")]
    )
    assert text.startswith("1 finding —")
    assert "oman national id (critical): 12****78" in text
    assert "12345678" not in text


def test_many_findings_are_capped_with_a_count() -> None:
    findings = [
        _Finding("injection.role_override", "high", matched_preview=f"x{i}") for i in range(5)
    ]
    text = _scan_result_text("prompt_injection", findings)
    assert text.startswith("5 findings —")
    assert "+2 more" in text


def test_an_unknown_scanner_still_reads_sensibly() -> None:
    assert _scan_result_text("mystery", []) == "clear — nothing found in the agent definition"
