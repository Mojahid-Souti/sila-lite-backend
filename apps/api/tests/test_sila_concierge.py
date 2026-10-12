"""SILA concierge — function-calling loop, role gating, WS handshake auth."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sila.adapters.auth.base import UserResult
from sila.adapters.llm.base import LLMDone, LLMTextDelta, LLMToolCall
from sila.core.constants import Role
from sila.main import app
from sila.services.sila.concierge_service import ConciergeService
from sila.services.sila.tool_registry import (
    TOOLS,
    ToolContext,
    execute_tool,
    tool_schemas,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_has_no_destructive_tools() -> None:
    """Security review: no delete/publish/approve/member tools may exist.
    Whole snake_case segments — plain substring flagged `remember` for
    containing "member" (false positive)."""
    forbidden = ("delete", "publish", "approve", "member", "role", "revoke")
    for name in TOOLS:
        segments = name.split("_")
        assert not any(seg == word for seg in segments for word in forbidden), name


def test_tool_schemas_shape() -> None:
    schemas = tool_schemas()
    # Hidden tools are deliberately NOT advertised to the model: confirm_send_email
    # is dispatched by the pause node only after the user clicks Send, so exposing
    # it would let the model bypass the outbound-email confirmation gate.
    hidden = {name for name, t in TOOLS.items() if t.extra.get("hidden")}
    assert hidden, "expected at least one hidden tool (the email confirm gate)"
    assert {s["function"]["name"] for s in schemas} == set(TOOLS) - hidden
    assert all(s["type"] == "function" for s in schemas)


def _ctx(role: Role) -> ToolContext:
    return ToolContext(
        db=AsyncMock(),
        user=UserResult(id=uuid.uuid4(), email="u@x.io", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=role,
    )


@pytest.mark.asyncio
async def test_role_gating_blocks_below_min_role() -> None:
    result = await execute_tool("hire_agent", _ctx(Role.VIEWER), {"agent_id": str(uuid.uuid4())})
    assert "doesn't permit" in result.content


@pytest.mark.asyncio
async def test_unknown_tool_is_refused() -> None:
    result = await execute_tool("delete_everything", _ctx(Role.ADMIN), {})
    assert "does not exist" in result.content


@pytest.mark.asyncio
async def test_navigate_emits_ui_command_with_workspace_path() -> None:
    ctx = _ctx(Role.VIEWER)
    result = await execute_tool("navigate", ctx, {"page": "workflows"})
    assert result.ui == {
        "command": "navigate",
        "path": f"/workspaces/{ctx.workspace_id}/workflows",
    }


# ---------------------------------------------------------------------------
# Concierge loop (fake gateway)
# ---------------------------------------------------------------------------


class _FakeGateway:
    """Scripted gateway: each call to stream_chat pops the next chunk list."""

    def __init__(self, rounds: list[list[Any]]) -> None:
        self._rounds = rounds
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, **_kw: Any) -> dict[str, Any]:
        raise NotImplementedError

    async def stream_chat(self, *, messages: Any, tools: Any = None, timeout_s: float = 0.0):
        self.calls.append({"messages": list(messages), "tools": tools})
        for chunk in self._rounds.pop(0):
            yield chunk


def _service(gateway: _FakeGateway) -> ConciergeService:
    svc = ConciergeService(AsyncMock(), gateway)
    svc._repo = MagicMock()
    svc._repo.add_message = AsyncMock()
    svc._repo.recent_messages = AsyncMock(return_value=[])
    svc._audit = MagicMock()
    svc._audit.record = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_plain_text_turn_streams_tokens_and_persists() -> None:
    gw = _FakeGateway([[LLMTextDelta("Hi "), LLMTextDelta("there."), LLMDone()]])
    svc = _service(gw)
    events: list[dict[str, Any]] = []

    async def emit(e: dict[str, Any]) -> None:
        events.append(e)

    reply = await svc.converse(
        session_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        user=UserResult(id=uuid.uuid4(), email="u@x.io", email_confirmed=True),
        role=Role.BUILDER,
        text="hello",
        emit=emit,
    )
    assert reply == "Hi there."
    assert [e["content"] for e in events if e["type"] == "token"] == ["Hi ", "there."]
    # user turn + sila reply persisted
    roles = [c.kwargs["role"] for c in svc._repo.add_message.call_args_list]
    assert roles == ["user", "sila"]


@pytest.mark.asyncio
async def test_tool_round_feeds_result_back_and_audits() -> None:
    gw = _FakeGateway(
        [
            [LLMToolCall(name="navigate", arguments={"page": "bazaar"}), LLMDone()],
            [LLMTextDelta("Opened the Bazaar for you."), LLMDone()],
        ]
    )
    svc = _service(gw)
    events: list[dict[str, Any]] = []

    async def emit(e: dict[str, Any]) -> None:
        events.append(e)

    reply = await svc.converse(
        session_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        user=UserResult(id=uuid.uuid4(), email="u@x.io", email_confirmed=True),
        role=Role.VIEWER,
        text="open the bazaar",
        emit=emit,
    )
    assert reply == "Opened the Bazaar for you."
    assert any(e["type"] == "ui" and e["command"] == "navigate" for e in events)
    tool_events = [e for e in events if e["type"] == "tool"]
    assert [e["status"] for e in tool_events] == ["running", "done"]
    svc._audit.record.assert_awaited_once()
    # Second LLM round saw the tool result message.
    assert gw.calls[1]["messages"][-1]["role"] == "tool"


# ---------------------------------------------------------------------------
# WebSocket handshake auth
# ---------------------------------------------------------------------------


def test_ws_rejects_garbage_token() -> None:
    with client.websocket_connect("/v1/sila/ws") as ws:
        ws.send_json({"type": "hello", "token": "garbage", "workspace_id": str(uuid.uuid4())})
        # Server closes with 4401; receive raises on closed socket.
        with pytest.raises(Exception):  # noqa: B017 — close surface differs by starlette version
            ws.receive_json()


def test_ws_rejects_missing_hello() -> None:
    with client.websocket_connect("/v1/sila/ws") as ws:
        ws.send_json({"type": "text", "content": "hi"})
        with pytest.raises(Exception):  # noqa: B017
            ws.receive_json()


# ---------------------------------------------------------------------------
# Language guard — Arabic in → Arabic out; NEVER Chinese (qwen drift)
# ---------------------------------------------------------------------------


def test_language_note_pins_arabic_for_arabic_input() -> None:
    from sila.services.sila.concierge_service import language_note

    note = language_note("كم عدد أيام الإجازة السنوية للموظف؟")
    assert "ARABIC" in note
    assert "بالعربية" in note
    assert "Chinese" in note  # the explicit never-Chinese line


def test_language_note_defaults_to_english() -> None:
    from sila.services.sila.concierge_service import language_note

    for text in ("How many leave days?", "", "hello 123"):
        note = language_note(text)
        assert "ENGLISH" in note
        assert "Chinese" in note
