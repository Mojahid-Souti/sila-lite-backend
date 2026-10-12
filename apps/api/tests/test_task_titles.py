"""Checklist rows name the work, not the stage.

A prompt that organised a request as "Phase 1 — Input / Phase 2 — Processing /
Phase 3 — Output" came back with those labels as the checklist rows, above a
second hand-written copy of the same list in prose. The prompt now forbids both;
this is the net under the titles.
"""

import uuid

import pytest

from sila.adapters.auth.base import UserResult
from sila.core.constants import Role
from sila.services.sila.tool_registry import ToolContext, _task_title, execute_tool


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        # The exact rows from the recorded run.
        ("Phase 1 — Input: Read Outlook inbox (last 7 days)", "Read Outlook inbox (last 7 days)"),
        ("Phase 2 — Processing: Build the dashboard inline", "Build the dashboard inline"),
        ("Step 2: Build the combined dashboard inline", "Build the combined dashboard inline"),
        (
            "Processing — combine the escalations with the data",
            "combine the escalations with the data",
        ),
        (
            "Requirements and data gathering: read the inbox",
            "read the inbox",
        ),
        ("1. Render the dashboard here", "Render the dashboard here"),
        ("3) Push the same dashboard to Grafana", "Push the same dashboard to Grafana"),
        # Already a real task: untouched, including its own numbers and dashes.
        (
            "Read the last 7 days of Outlook and pull the escalations",
            "Read the last 7 days of Outlook and pull the escalations",
        ),
        ("Push the same dashboard to Grafana", "Push the same dashboard to Grafana"),
        (
            "Build the six-slide pack — escalations first",
            "Build the six-slide pack — escalations first",
        ),
        # Nothing but a label: left alone. Rewriting it to "Output" is no
        # clearer and would break the `done` matching.
        ("Phase 3 — Output", "Phase 3 — Output"),
        ("Input", "Input"),
        # Whitespace is normalised either way.
        ("  Phase 1 —  Input:   read   the inbox ", "read the inbox"),
    ],
)
def test_stage_labels_are_stripped_and_real_titles_are_not(raw: str, shown: str) -> None:
    assert _task_title(raw) == shown


async def test_the_published_plan_carries_the_cleaned_titles() -> None:
    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )
    result = await execute_tool(
        "track_tasks",
        ctx,
        {
            "tasks": [
                "Phase 1 — Input: Read the Outlook inbox",
                "Phase 2 — Processing: Render the dashboard",
                "Phase 3 — Output: Build the deck",
            ]
        },
    )
    titles = [t["title"] for t in ctx.build_state["tasks"]]
    assert titles == ["Read the Outlook inbox", "Render the dashboard", "Build the deck"]
    assert result.meta is not None
    assert [t["title"] for t in result.meta["tasks"]] == titles


async def test_done_still_matches_after_cleaning() -> None:
    """The model marks a step done by sending its title back. Cleaning has to be
    idempotent, or the row it just finished never ticks."""
    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )
    tasks = ["Phase 1 — Input: Read the Outlook inbox", "Render the dashboard"]
    await execute_tool("track_tasks", ctx, {"tasks": tasks})
    # It sends the ORIGINAL label back in `done` — that must still tick.
    await execute_tool(
        "track_tasks", ctx, {"tasks": tasks, "done": ["Phase 1 — Input: Read the Outlook inbox"]}
    )
    by_title = {t["title"]: t["done"] for t in ctx.build_state["tasks"]}
    assert by_title == {"Read the Outlook inbox": True, "Render the dashboard": False}
