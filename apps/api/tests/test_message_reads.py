"""Reading a mailbox must not turn into charting one.

One recorded turn: the inbox held ONE message, SILA reported "Read 5 emails",
read the inbox three times, refused `analyze_data` three times, then built a
one-row dashboard of sender addresses — while the spreadsheet the user had
attached in the same message went untouched. Four separate defects, pinned here.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sila.adapters.auth.base import UserResult
from sila.core.constants import Role
from sila.services.connectors import reads
from sila.services.sila.tool_registry import (
    ToolContext,
    _latest_data_file,
    _pick_data_file,
    _stash_virtual_file,
    execute_tool,
)

_ATTACHED = {"name": "omantel-demo-revenue-2025.csv", "content": "month,revenue\n2025-01,10"}


def _ctx() -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )


# --- the count that lied ----------------------------------------------------


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"payload": {}}
    monkeypatch.setattr(reads, "_binding_for", AsyncMock(return_value=(object(), object())))
    monkeypatch.setattr(
        reads, "_proxy_get", AsyncMock(side_effect=lambda *a, **k: state["payload"])
    )
    monkeypatch.setattr(reads, "_audit_read", AsyncMock(return_value=None))
    return state


def _one_mail_with_newlines() -> dict[str, Any]:
    """A single message whose preview and body carry newlines — the shape that
    made a newline tally over the CSV report five emails instead of one."""
    return {
        "value": [
            {
                "from": {"emailAddress": {"address": "desk@example.com"}},
                "subject": "Retention escalations",
                "receivedDateTime": "2026-09-12T08:00:00Z",
                "bodyPreview": "Hi team,\n\nTwo escalations:\n1. one\n2. two\n",
                "body": {"content": "<p>Hi team,</p><p>Two escalations:</p><p>1. one</p>"},
            }
        ]
    }


async def test_one_email_is_reported_as_one(graph: dict[str, Any]) -> None:
    graph["payload"] = _one_mail_with_newlines()
    ctx = _ctx()
    result = await execute_tool("read_outlook_emails", ctx, {})
    assert result.summary == "Read 1 email"
    assert "Read 1 email." in result.content


async def test_the_count_matches_the_progress_total(graph: dict[str, Any]) -> None:
    """The counter and the sentence come from one number now. They disagreed:
    "Read 1 of 1 · 100%" above "Read 5 emails"."""
    payload = _one_mail_with_newlines()
    payload["value"] = payload["value"] * 3
    graph["payload"] = payload
    ctx = _ctx()
    frames: list[tuple[int, int]] = []
    ctx.progress = lambda note, extra=None: (
        frames.append((extra["done"], extra["total"])) if extra else None
    )
    result = await execute_tool("read_outlook_emails", ctx, {})
    assert result.summary == "Read 3 emails"
    assert frames[-1] == (3, 3)


# --- the instruction that caused the charting -------------------------------


async def test_a_mailbox_read_forbids_charting_itself(graph: dict[str, Any]) -> None:
    """The generic note said "the data is now attached… ask which output", which
    is how an inbox became a dashboard. Mail says the opposite, by name."""
    graph["payload"] = _one_mail_with_newlines()
    result = await execute_tool("read_outlook_emails", _ctx(), {})
    assert "NOT a dataset" in result.content
    assert "create_data_dashboard" in result.content and "analyze_data" in result.content
    assert "quote" in result.content


# --- which file a data tool acts on -----------------------------------------


def test_an_attachment_beats_a_mail_export_even_when_the_model_names_it() -> None:
    ctx = _ctx()
    ctx.build_state["uploaded_files"] = [dict(_ATTACHED)]
    _stash_virtual_file(ctx, "outlook-emails.csv", "from,subject\na,b")
    files = ctx.build_state["uploaded_files"]
    # The model asked for the mail file by name; the user's spreadsheet wins.
    assert _pick_data_file(ctx, files, "outlook-emails.csv")["name"] == _ATTACHED["name"]
    # With no attachment present, the named file is honoured.
    only_mail = [f for f in files if f.get("virtual")]
    assert _pick_data_file(ctx, only_mail, "outlook-emails.csv")["name"] == "outlook-emails.csv"


def test_naming_the_attachment_still_selects_it() -> None:
    ctx = _ctx()
    ctx.build_state["uploaded_files"] = [
        {"name": "other.csv", "content": "a,b\n1,2"},
        dict(_ATTACHED),
    ]
    assert (
        _pick_data_file(ctx, ctx.build_state["uploaded_files"], "omantel")["name"]
        == (_ATTACHED["name"])
    )


def test_analysis_uses_the_same_rule_as_every_other_data_tool() -> None:
    """`analyze_data` took the most recent file, landed on the mail export,
    found no numeric column and refused — three times."""
    ctx = _ctx()
    ctx.build_state["uploaded_files"] = [dict(_ATTACHED)]
    _stash_virtual_file(ctx, "outlook-emails.csv", "from,subject\na,b")
    chosen = _latest_data_file(ctx)
    assert chosen is not None and chosen["name"] == _ATTACHED["name"]


def test_no_data_file_at_all_is_still_none() -> None:
    assert _latest_data_file(_ctx()) is None


# --- the one-row dashboard --------------------------------------------------


def test_a_single_row_is_refused_with_a_reason() -> None:
    from sila.services.sila.data_dashboard import build_data_dashboard

    with pytest.raises(ValueError, match="nothing to chart"):
        build_data_dashboard("outlook-emails.csv", "from,subject\ndesk@example.com,Escalations")


def test_two_rows_still_chart() -> None:
    """Two is deliberately allowed — several fixtures and one real behaviour
    (a series too short to compare) are specified there."""
    from sila.services.sila.data_dashboard import build_data_dashboard

    dash = build_data_dashboard("small.csv", "region,revenue\nNorth,10\nSouth,20")
    assert dash["rows"] == 2 and dash["charts"]


# --- the re-reading ---------------------------------------------------------


async def test_the_text_is_in_the_result_so_no_second_read_is_needed(
    graph: dict[str, Any],
) -> None:
    """Telling the model the text was "in a file" without giving it the text is
    what made it read the inbox after every step: nothing to quote, so fetch
    again. The result now carries the messages themselves."""
    graph["payload"] = _one_mail_with_newlines()
    result = await execute_tool("read_outlook_emails", _ctx(), {})
    assert "Subject: Retention escalations" in result.content
    assert "Two escalations" in result.content
    assert "do not read the inbox again" in result.content


async def test_a_repeat_read_is_served_from_memory(graph: dict[str, Any]) -> None:
    graph["payload"] = _one_mail_with_newlines()
    ctx = _ctx()
    first = await execute_tool("read_outlook_emails", ctx, {"since_days": 7})
    calls_after_first = reads._proxy_get.await_count  # type: ignore[attr-defined]
    second = await execute_tool("read_outlook_emails", ctx, {"since_days": 7})
    assert reads._proxy_get.await_count == calls_after_first  # type: ignore[attr-defined]
    assert second.summary == "Using the 1 email already read"
    assert "Two escalations" in second.content
    assert first.summary == "Read 1 email"


async def test_different_arguments_are_a_different_read(graph: dict[str, Any]) -> None:
    graph["payload"] = _one_mail_with_newlines()
    ctx = _ctx()
    await execute_tool("read_outlook_emails", ctx, {"since_days": 7})
    before = reads._proxy_get.await_count  # type: ignore[attr-defined]
    await execute_tool("read_outlook_emails", ctx, {"since_days": 30})
    assert reads._proxy_get.await_count == before + 1  # type: ignore[attr-defined]


async def test_refresh_forces_a_fetch(graph: dict[str, Any]) -> None:
    graph["payload"] = _one_mail_with_newlines()
    ctx = _ctx()
    await execute_tool("read_outlook_emails", ctx, {})
    before = reads._proxy_get.await_count  # type: ignore[attr-defined]
    result = await execute_tool("read_outlook_emails", ctx, {"refresh": True})
    assert reads._proxy_get.await_count == before + 1  # type: ignore[attr-defined]
    assert result.summary == "Read 1 email"


async def test_an_empty_window_is_an_answer_not_a_failure(graph: dict[str, Any]) -> None:
    """A week with no mail showed "Mail read failed" in the chat."""
    graph["payload"] = {"value": []}
    result = await execute_tool("read_outlook_emails", _ctx(), {"since_days": 7})
    assert result.summary == "No emails in the last 7 days"
    assert "larger since_days" in result.content


async def test_a_real_read_error_still_says_failed(
    graph: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sila.services.connectors.connector_service import ConnectorError

    monkeypatch.setattr(reads, "_proxy_get", AsyncMock(side_effect=ConnectorError("Graph 503")))
    result = await execute_tool("read_outlook_emails", _ctx(), {})
    assert result.summary == "Mail read failed"
