"""The counted read callback on the inbox and channel readers.

The trace shows "Reading 3 of 23 · 13%" and a scrolling list only if the reader
fires `on_item` once per message with the running count and the item just
handled. These pin that contract for both readers, with Graph mocked out.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sila.services.connectors import reads

_USER = uuid.uuid4()
_WS = uuid.uuid4()


def _mail(n: int) -> dict[str, Any]:
    return {
        "value": [
            {
                "from": {"emailAddress": {"address": f"sender{i}@example.com"}},
                "subject": f"Subject {i}",
                "receivedDateTime": "2026-09-12T08:00:00Z",
                "bodyPreview": "p",
                "body": {"content": f"<p>body {i}</p>"},
            }
            for i in range(1, n + 1)
        ]
    }


def _channel(n: int) -> dict[str, Any]:
    return {
        "value": [
            {
                "from": {"user": {"displayName": f"Person {i}"}},
                "createdDateTime": "2026-09-12T08:00:00Z",
                "body": {"content": f"<p>message {i}</p>"},
            }
            for i in range(1, n + 1)
        ]
    }


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the binding lookup, the Graph GET and the audit write."""
    state: dict[str, Any] = {"payload": {}}
    monkeypatch.setattr(reads, "_binding_for", AsyncMock(return_value=(object(), object())))
    monkeypatch.setattr(
        reads, "_proxy_get", AsyncMock(side_effect=lambda *a, **k: state["payload"])
    )
    monkeypatch.setattr(reads, "_audit_read", AsyncMock(return_value=None))
    return state


async def test_outlook_reports_every_message_in_order(graph: dict[str, Any]) -> None:
    graph["payload"] = _mail(3)
    seen: list[tuple[int, int, dict[str, Any]]] = []
    await reads.read_outlook_emails(
        None,  # type: ignore[arg-type]
        user_id=_USER,
        workspace_id=_WS,
        on_item=lambda done, total, item: seen.append((done, total, item)),
    )
    assert [(d, t) for d, t, _ in seen] == [(1, 3), (2, 3), (3, 3)]
    assert seen[0][2] == {"title": "Subject 1", "meta": "sender1@example.com"}


async def test_outlook_without_a_callback_is_unchanged(graph: dict[str, Any]) -> None:
    graph["payload"] = _mail(2)
    csv_name, csv_text, txt_name, txt_text = await reads.read_outlook_emails(
        None,  # type: ignore[arg-type]
        user_id=_USER,
        workspace_id=_WS,
    )
    assert csv_name == "outlook-emails.csv" and csv_text.count("\n") == 3
    assert "Subject 2" in txt_text


async def test_teams_reports_author_and_text(graph: dict[str, Any]) -> None:
    graph["payload"] = _channel(2)
    seen: list[tuple[int, int, dict[str, Any]]] = []
    await reads.read_teams_messages(
        None,  # type: ignore[arg-type]
        user_id=_USER,
        workspace_id=_WS,
        team_id="team-000001",
        channel_id="19:channel@thread.tacv2",
        on_item=lambda done, total, item: seen.append((done, total, item)),
    )
    assert [(d, t) for d, t, _ in seen] == [(1, 2), (2, 2)]
    assert seen[1][2] == {"title": "message 2", "meta": "Person 2"}


async def test_an_empty_subject_still_has_a_title(graph: dict[str, Any]) -> None:
    payload = _mail(1)
    payload["value"][0]["subject"] = ""
    graph["payload"] = payload
    seen: list[dict[str, Any]] = []
    await reads.read_outlook_emails(
        None,  # type: ignore[arg-type]
        user_id=_USER,
        workspace_id=_WS,
        on_item=lambda _d, _t, item: seen.append(item),
    )
    assert seen[0]["title"] == "(no subject)"
