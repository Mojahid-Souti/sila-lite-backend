"""Supervisor graph: a pausing tool interrupts; a resume continues the turn."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from sila.adapters.auth.base import UserResult
from sila.adapters.llm.base import LLMChunk, LLMDone, LLMTextDelta, LLMToolCall
from sila.core.constants import Role
from sila.services.sila.orchestrator.graph import GraphDeps, build_supervisor_graph


class _FakeGateway:
    """Round 1 → an ask_user tool call; round 2 (after resume) → a plain reply."""

    def __init__(self) -> None:
        self.rounds = 0

    async def stream_chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout_s: float = 60.0,
    ) -> AsyncIterator[LLMChunk]:
        self.rounds += 1
        if self.rounds == 1:
            yield LLMToolCall(
                name="ask_user",
                arguments={
                    "questions": [{"question": "Out-of-docs?", "options": ["Say IDK", "Guess"]}]
                },
            )
            yield LLMDone()
        else:
            yield LLMTextDelta(text="All set — building it now.")
            yield LLMDone()

    async def complete_json(self, *, system_prompt: str, user_prompt: str, timeout_s: float = 30.0):
        return {}


class _FakeAudit:
    _db = None

    async def record(self, **kwargs: Any) -> None:  # noqa: ANN401
        return None


def _graph() -> Any:
    deps = GraphDeps(
        gateway=_FakeGateway(),  # type: ignore[arg-type]
        audit=_FakeAudit(),  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        role=Role.BUILDER,
        workspace_id=uuid.uuid4(),
    )
    return build_supervisor_graph(deps, InMemorySaver())


@pytest.mark.asyncio
async def test_pausing_tool_interrupts_then_resumes() -> None:
    graph = _graph()
    config = {"configurable": {"thread_id": "t1"}}

    # Turn 1 — runs to the ask_user interrupt and pauses.
    events: list[dict[str, Any]] = []
    async for ev in graph.astream(
        {"messages": [{"role": "user", "content": "build an HR agent from my handbook"}]},
        config,
        stream_mode="custom",
    ):
        events.append(ev)

    # The chooser was streamed as a tool step, and the graph is paused on `pause`.
    assert any(e.get("type") == "tool" and e.get("name") == "ask_user" for e in events)
    state = await graph.aget_state(config)
    assert state.next == ("pause",)
    interrupt_payload = state.tasks[0].interrupts[0].value
    assert interrupt_payload["command"] == "ask_user"
    assert interrupt_payload["questions"][0]["question"] == "Out-of-docs?"

    # Resume with the user's answer → the turn finishes with the reply.
    after: list[dict[str, Any]] = []
    async for ev in graph.astream(
        Command(resume="My answers — Out-of-docs? = Say IDK"), config, stream_mode="custom"
    ):
        after.append(ev)

    assert any(e.get("type") == "token" for e in after)
    final = await graph.aget_state(config)
    assert final.next == ()  # turn complete
    assert "building it now" in final.values["reply"]
