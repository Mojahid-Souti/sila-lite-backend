"""The graph refuses a tool call while the previous step is unexplained, and a
ticked row stays ticked.

The recorded run: inbox read, straight to the web search, straight to reading
pages — not a sentence to the reader in between — and then the model
re-published the plan with a shorter `done` list and un-ticked the search it
had just run, so it ran it again. The advisory instruction was walked past;
this is the gate that cannot be.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sila.adapters.auth.base import UserResult
from sila.core.constants import Role
from sila.services.sila.orchestrator.graph import (
    _MAX_FEEDBACK_STRIKES,
    _after_tool,
    _needs_feedback_first,
)
from sila.services.sila.tool_registry import ToolContext, execute_tool

THREE = (
    "The inbox held one message from the retention desk. It names two customers, "
    "one enterprise account in Dhofar with three outages and one consumer billing "
    "dispute of RO 84.500. Both need a decision this week, which is what the pack is for."
)


@dataclass
class _Result:
    content: str = "Read 1 email."


def _open_plan() -> dict[str, Any]:
    return {
        "tasks": [
            {"title": "Read the inbox", "done": True},
            {"title": "Search once for the public figure", "done": False},
        ]
    }


# --- the gate ---------------------------------------------------------------


def test_nothing_owed_means_nothing_refused() -> None:
    assert _needs_feedback_first("Next.", ["web_search"], {}) is None


def test_a_tool_call_with_a_one_liner_is_refused() -> None:
    state = {"owe_feedback": "read_outlook_emails"}
    nudge = _needs_feedback_first("Now searching.", ["web_search"], state)
    assert nudge is not None
    assert "read outlook emails" in nudge and "three full sentences" in nudge
    assert "Do not call any tool in this message" in nudge
    assert state["feedback_strikes"] == 1
    assert state["owe_feedback"] == "read_outlook_emails", "still owed"


def test_three_real_sentences_clear_the_debt_and_let_the_call_run() -> None:
    state = {"owe_feedback": "read_outlook_emails", "feedback_strikes": 1}
    assert _needs_feedback_first(THREE, ["web_search"], state) is None
    assert "owe_feedback" not in state and "feedback_strikes" not in state


def test_three_tiny_sentences_do_not_count() -> None:
    """ "Done. Ok. Next." is three sentences and says nothing."""
    state = {"owe_feedback": "web_search"}
    assert _needs_feedback_first("Done. Ok. Next.", ["fetch_url"], state) is not None


def test_a_plan_update_alone_passes_but_the_debt_remains() -> None:
    state = {"owe_feedback": "web_search"}
    assert _needs_feedback_first("", ["track_tasks"], state) is None
    assert state["owe_feedback"] == "web_search"


def test_prose_with_no_tool_call_is_never_refused() -> None:
    """The model may write a short line and stop; that ends the round, and the
    explanation is still owed before its next tool call."""
    state = {"owe_feedback": "web_search"}
    assert _needs_feedback_first("One moment.", [], state) is None
    assert state["owe_feedback"] == "web_search"


def test_the_gate_gives_up_after_the_limit_rather_than_wedging_the_turn() -> None:
    state = {"owe_feedback": "web_search"}
    refusals = 0
    for _ in range(_MAX_FEEDBACK_STRIKES + 1):
        if _needs_feedback_first("Go.", ["fetch_url"], state) is not None:
            refusals += 1
    assert refusals == _MAX_FEEDBACK_STRIKES
    assert "owe_feedback" not in state, "exhausted: the call goes through"


def test_a_finished_step_creates_the_debt() -> None:
    state = _open_plan()
    _after_tool("web_search", _Result("8 hits"), state, lambda f: None)
    assert state["owe_feedback"] == "web_search"


def test_the_plan_tool_never_creates_a_debt() -> None:
    state = _open_plan()
    _after_tool("track_tasks", _Result("Plan updated"), state, lambda f: None)
    assert "owe_feedback" not in state


# --- a tick is monotonic ------------------------------------------------------


def _ctx() -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )


async def test_a_row_ticked_by_the_orchestrator_survives_a_shorter_done_list() -> None:
    ctx = _ctx()
    tasks = ["Read the inbox", "Search once for the public figure", "Render the dashboard"]
    await execute_tool("track_tasks", ctx, {"tasks": tasks})
    # The orchestrator ticks the search the moment the tool finishes…
    ctx.build_state["tasks"][1]["done"] = True
    # …then the model re-publishes, remembering only the first row.
    await execute_tool("track_tasks", ctx, {"tasks": tasks, "done": ["Read the inbox"]})
    assert [t["done"] for t in ctx.build_state["tasks"]] == [True, True, False]


async def test_a_fresh_plan_starts_with_no_debt() -> None:
    ctx = _ctx()
    ctx.build_state["owe_feedback"] = "web_search"
    ctx.build_state["feedback_strikes"] = 1
    await execute_tool("track_tasks", ctx, {"tasks": ["Read the inbox", "Chart it"]})
    assert "owe_feedback" not in ctx.build_state
    assert "feedback_strikes" not in ctx.build_state
