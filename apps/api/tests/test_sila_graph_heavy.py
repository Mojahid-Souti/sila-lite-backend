"""Heavy behavioural tests for the SILA LangGraph orchestrator.

Deterministic + fast: a scriptable fake gateway drives the rounds, and only the
side-effect-free tools (navigate, ask_user, request_documents) or the role-gate
short-circuit are exercised, so no Ollama / DB / n8n is needed. Covers routing,
interrupt/resume, docs-before-questions, multi-round, durable resume from a fresh
graph instance, recursion bound, LLM-error handling, history accumulation, and
role gating.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from sila.adapters.auth.base import UserResult
from sila.adapters.llm.base import LLMChunk, LLMDone, LLMGatewayError, LLMTextDelta, LLMToolCall
from sila.core.constants import Role
from sila.services.sila.orchestrator.graph import GraphDeps, build_supervisor_graph


# --- scripting helpers -----------------------------------------------------
def txt(s: str) -> LLMTextDelta:
    return LLMTextDelta(text=s)


def call(tool: str, **args: Any) -> LLMToolCall:
    return LLMToolCall(name=tool, arguments=args)


def done() -> LLMDone:
    return LLMDone()


class ScriptedGateway:
    """Each stream_chat call yields the next round's chunks; records messages seen."""

    def __init__(self, script: list[list[LLMChunk]], *, loop_last: bool = False) -> None:
        self._script = list(script)
        self._loop_last = loop_last
        self.seen: list[list[dict[str, Any]]] = []
        self.raise_error = False

    async def stream_chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout_s: float = 60.0,
    ) -> AsyncIterator[LLMChunk]:
        self.seen.append(messages)
        if self.raise_error:
            raise LLMGatewayError("boom")
        if self._script:
            chunks = (
                self._script[0]
                if (self._loop_last and len(self._script) == 1)
                else self._script.pop(0)
            )
        else:
            chunks = [done()]
        for c in chunks:
            yield c

    async def complete_json(self, *, system_prompt: str, user_prompt: str, timeout_s: float = 30.0):
        return {}


class _FakeAudit:
    _db = None

    async def record(self, **kwargs: Any) -> None:  # noqa: ANN401
        return None


def make(
    script: list[list[LLMChunk]],
    *,
    role: Role = Role.BUILDER,
    saver: Any = None,
    loop_last: bool = False,
) -> tuple[Any, ScriptedGateway]:
    gw = ScriptedGateway(script, loop_last=loop_last)
    deps = GraphDeps(
        gateway=gw,  # type: ignore[arg-type]
        audit=_FakeAudit(),  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        role=role,
        workspace_id=uuid.uuid4(),
    )
    return build_supervisor_graph(deps, saver or InMemorySaver()), gw


def cfg(thread: str = "t", limit: int = 24) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread}, "recursion_limit": limit}


async def drive(graph: Any, payload: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async for ev in graph.astream(payload, config, stream_mode="custom"):
        if isinstance(ev, dict):
            out.append(ev)
    return out


def user(text: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


# --- tests -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_plain_reply_no_tools() -> None:
    graph, _ = make([[txt("Hello there."), done()]])
    config = cfg()
    evs = await drive(graph, user("hi"), config)
    assert "".join(e["content"] for e in evs if e["type"] == "token") == "Hello there."
    state = await graph.aget_state(config)
    assert state.next == ()
    assert state.values["reply"] == "Hello there."


@pytest.mark.asyncio
async def test_non_pausing_tool_then_reply() -> None:
    graph, gw = make(
        [[call("navigate", page="bazaar"), done()], [txt("Opened the Bazaar."), done()]]
    )
    config = cfg()
    evs = await drive(graph, user("open bazaar"), config)
    assert any(
        e["type"] == "tool" and e["name"] == "navigate" and e["status"] == "done" for e in evs
    )
    assert any(e["type"] == "ui" and e.get("command") == "navigate" for e in evs)
    state = await graph.aget_state(config)
    assert state.next == ()
    assert "Bazaar" in state.values["reply"]
    assert len(gw.seen) == 2  # two LLM rounds (decide tool, then reply)


@pytest.mark.asyncio
async def test_ask_user_interrupts_then_resumes() -> None:
    graph, _ = make(
        [
            [
                call(
                    "ask_user",
                    questions=[{"question": "Out-of-docs?", "options": ["IDK", "Guess"]}],
                ),
                done(),
            ],
            [txt("Great — building it."), done()],
        ]
    )
    config = cfg()
    await drive(graph, user("build hr agent"), config)
    state = await graph.aget_state(config)
    assert state.next == ("pause",)
    payload = state.tasks[0].interrupts[0].value
    assert payload["command"] == "ask_user"
    assert payload["questions"][0]["question"] == "Out-of-docs?"

    await drive(graph, Command(resume="IDK"), config)
    final = await graph.aget_state(config)
    assert final.next == ()
    assert "building it" in final.values["reply"]


@pytest.mark.asyncio
async def test_request_documents_interrupts_then_resumes() -> None:
    graph, _ = make(
        [
            [call("request_documents", prompt="Share the handbook"), done()],
            [txt("Got the docs."), done()],
        ]
    )
    config = cfg()
    await drive(graph, user("build from my docs"), config)
    state = await graph.aget_state(config)
    assert state.next == ("pause",)
    payload = state.tasks[0].interrupts[0].value
    assert payload["command"] == "open_modal"
    assert payload["modal"] == "upload"

    await drive(graph, Command(resume="I've uploaded: hr.pdf"), config)
    final = await graph.aget_state(config)
    assert final.next == ()
    assert "Got the docs" in final.values["reply"]


@pytest.mark.asyncio
async def test_docs_collected_before_questions_when_both_emitted() -> None:
    # Model emits ask_user AND request_documents in the same round (wrong order).
    graph, _ = make(
        [
            [
                call("ask_user", questions=[{"question": "Tone?", "options": ["A", "B"]}]),
                call("request_documents", prompt="docs"),
                done(),
            ],
            [
                call(
                    "ask_user",
                    questions=[{"question": "Out-of-docs?", "options": ["IDK", "Guess"]}],
                ),
                done(),
            ],
            [txt("Done."), done()],
        ]
    )
    config = cfg()
    await drive(graph, user("build"), config)
    # First interrupt must be the upload, not the questions.
    p1 = (await graph.aget_state(config)).tasks[0].interrupts[0].value
    assert p1["command"] == "open_modal"

    await drive(graph, Command(resume="uploaded"), config)
    # Now the questions.
    p2 = (await graph.aget_state(config)).tasks[0].interrupts[0].value
    assert p2["command"] == "ask_user"

    await drive(graph, Command(resume="IDK"), config)
    assert (await graph.aget_state(config)).next == ()


@pytest.mark.asyncio
async def test_two_non_pausing_rounds() -> None:
    graph, gw = make(
        [
            [call("navigate", page="bazaar"), done()],
            [call("navigate", page="agents"), done()],
            [txt("All done."), done()],
        ]
    )
    config = cfg()
    evs = await drive(graph, user("go"), config)
    ui = [e for e in evs if e["type"] == "ui" and e.get("command") == "navigate"]
    assert len(ui) == 2
    assert len(gw.seen) == 3
    assert (await graph.aget_state(config)).values["reply"] == "All done."


@pytest.mark.asyncio
async def test_durable_resume_from_a_fresh_graph_instance() -> None:
    saver = InMemorySaver()  # shared store = stand-in for the Postgres checkpointer
    config = cfg(thread="durable")

    # Connection 1 pauses at ask_user.
    g1, _ = make(
        [[call("ask_user", questions=[{"question": "Q?", "options": ["A", "B"]}]), done()]],
        saver=saver,
    )
    await drive(g1, user("build"), config)
    assert (await g1.aget_state(config)).next == ("pause",)

    # Connection 2 — a brand-new graph + gateway over the SAME store/thread — resumes.
    g2, _ = make([[txt("resumed and finished."), done()]], saver=saver)
    await drive(g2, Command(resume="A"), config)
    final = await g2.aget_state(config)
    assert final.next == ()
    assert "resumed and finished" in final.values["reply"]


@pytest.mark.asyncio
async def test_recursion_limit_bounds_runaway_loops() -> None:
    # A model that never stops calling a tool must hit the limit, not hang.
    graph, _ = make([[call("navigate", page="bazaar"), done()]], loop_last=True)
    with pytest.raises(GraphRecursionError):
        await drive(graph, user("loop forever"), cfg(thread="r", limit=6))


@pytest.mark.asyncio
async def test_llm_error_emits_error_frame_and_ends() -> None:
    graph, gw = make([[txt("never"), done()]])
    gw.raise_error = True
    config = cfg(thread="err")
    evs = await drive(graph, user("hi"), config)
    assert any(e["type"] == "error" and e["message"] == "brain_unavailable" for e in evs)
    assert (await graph.aget_state(config)).next == ()


@pytest.mark.asyncio
async def test_history_accumulates_across_turns() -> None:
    graph, gw = make([[txt("first reply"), done()], [txt("second reply"), done()]])
    config = cfg(thread="hist")
    await drive(graph, user("first question"), config)
    await drive(graph, user("second question"), config)
    # The 2nd round's prompt must carry turn-1's user + assistant messages.
    second_round = gw.seen[1]
    contents = [m.get("content") for m in second_round]
    assert "first question" in contents
    assert "first reply" in contents
    assert "second question" in contents


@pytest.mark.asyncio
async def test_role_gate_refuses_builder_tool_for_viewer() -> None:
    # A Viewer asking to build: execute_tool short-circuits with a refusal (no n8n).
    graph, gw = make(
        [
            [call("create_agent_workflow", name="X", purpose="Y"), done()],
            [txt("Sorry, can't."), done()],
        ],
        role=Role.VIEWER,
    )
    config = cfg(thread="role")
    await drive(graph, user("build an agent"), config)
    # The refusal text is fed back to the model on the next round.
    tool_msgs = [m for m in gw.seen[1] if m.get("role") == "tool"]
    assert tool_msgs and "permit" in tool_msgs[-1]["content"].lower()
    assert (await graph.aget_state(config)).next == ()


# --- B1: visible vetting node -----------------------------------------------
def _stub_build_and_vetting(monkeypatch: Any, *, outcome: str) -> str:
    """Make `create_agent_workflow` return a vetted_agent_id without touching n8n,
    and `scan_streamed` stream four checks + the given outcome without a DB."""
    import sila.services.sila.orchestrator.graph as gmod
    from sila.services.sila.tool_registry import ToolResult

    agent_id = str(uuid.uuid4())

    async def fake_execute_tool(name: str, ctx: Any, args: dict[str, Any]) -> ToolResult:
        if name == "create_agent_workflow":
            return ToolResult(
                content="Created draft agent 'HR'.",
                summary="Created draft agent: HR",
                ui={"command": "open_canvas", "workflow_id": "wf1"},
                meta={"vetted_agent_id": agent_id},
            )
        return ToolResult(content="ok")

    monkeypatch.setattr(gmod, "execute_tool", fake_execute_tool)

    class FakeAgentRepo:
        def __init__(self, _db: Any) -> None: ...
        async def get(self, aid: Any, wid: Any) -> Any:  # noqa: ANN401
            return SimpleNamespace(id=aid, workspace_id=wid)

    class FakeRun:
        outcome = None

    class FakeVetting:
        def __init__(self, _db: Any) -> None: ...
        async def scan_streamed(self, *, agent: Any, actor_id: Any, on_step: Any) -> Any:  # noqa: ANN401
            for nm in ("pii", "prompt_injection", "llm_review", "workflow_rules"):
                on_step(nm, [])
            run = FakeRun()
            run.outcome = outcome
            return run

    monkeypatch.setattr("sila.db.repositories.agent.AgentRepository", FakeAgentRepo)
    monkeypatch.setattr("sila.services.vetting_service.VettingService", FakeVetting)
    return agent_id


@pytest.mark.asyncio
async def test_build_streams_visible_vetting_then_replies(monkeypatch: Any) -> None:
    _stub_build_and_vetting(monkeypatch, outcome="auto_approved")
    graph, gw = make(
        [
            [call("create_agent_workflow", name="HR", purpose="answer HR questions"), done()],
            [txt("Your HR agent is ready!"), done()],
        ]
    )
    config = cfg(thread="vet-ok")
    evs = await drive(graph, user("build an HR agent"), config)

    # The four checks streamed as steps (stable ids; the frontend maps label+icon).
    names = {e.get("name") for e in evs if e["type"] == "tool"}
    assert {"pii", "prompt_injection", "llm_review", "workflow_rules"} <= names
    assert any(e["type"] == "tool" and e["name"] == "security_review" for e in evs)
    # The canvas opened from the build tool.
    assert any(e["type"] == "ui" and e.get("command") == "open_canvas" for e in evs)

    state = await graph.aget_state(config)
    assert state.next == ()
    assert state.values.get("to_vet") is None  # cleared
    assert "ready" in state.values["reply"].lower()
    # The agent composed its closing line from the clean-pass outcome message.
    tool_msgs = [m for m in gw.seen[1] if m.get("role") == "tool"]
    assert any("ready in the workspace" in m["content"] for m in tool_msgs)


@pytest.mark.asyncio
async def test_pausing_tool_without_ui_recovers_instead_of_ending() -> None:
    # A weak model emits an invalid ask_user (no questions) → the tool returns no UI.
    # The graph must NOT end silently mid-pause; it loops back so the model recovers.
    graph, gw = make(
        [
            [call("ask_user", questions=[]), done()],
            [txt("Sure — what should it do when it can't find an answer?"), done()],
        ]
    )
    config = cfg(thread="retry")
    await drive(graph, user("build something"), config)
    state = await graph.aget_state(config)
    assert state.next == ()
    assert "what should it do" in state.values["reply"]
    assert len(gw.seen) == 2  # looped back to the agent once


@pytest.mark.asyncio
async def test_request_credentials_interrupts_with_incognito_payload() -> None:
    # A build that needs a Slack token pauses for the masked Incognito panel.
    graph, _ = make(
        [
            [call("request_credentials", provider="slack"), done()],
            [txt("Connected — building it."), done()],
        ]
    )
    config = cfg(thread="cred")
    await drive(graph, user("build an agent that posts to slack"), config)
    state = await graph.aget_state(config)
    assert state.next == ("pause",)
    payload = state.tasks[0].interrupts[0].value
    assert payload["command"] == "request_credentials"
    assert payload["provider"] == "slack"
    assert payload["fields"][0]["secret"] is True

    # Resume with a NON-secret handle (what the WS sends after storing the secret).
    await drive(graph, Command(resume="Connected Slack securely."), config)
    final = await graph.aget_state(config)
    assert final.next == ()
    assert "building it" in final.values["reply"]


@pytest.mark.asyncio
async def test_build_pauses_for_missing_credential(monkeypatch: Any) -> None:
    # The build needs a Slack token but none is collected → the build tool returns a
    # request_credentials UI; the graph must PAUSE (Incognito panel), not keep going.
    import sila.services.sila.orchestrator.graph as gmod
    from sila.services.sila.tool_registry import ToolResult

    async def fake_execute_tool(name: str, ctx: Any, args: dict[str, Any]) -> ToolResult:
        if name == "create_agent_workflow":
            return ToolResult(
                content="Need the Slack token first.",
                summary="Need Slack credentials",
                ui={
                    "command": "request_credentials",
                    "request_id": "r1",
                    "provider": "slack",
                    "label": "Slack",
                    "fields": [{"key": "accessToken", "label": "Slack bot token", "secret": True}],
                },
            )
        return ToolResult(content="ok")

    monkeypatch.setattr(gmod, "execute_tool", fake_execute_tool)
    graph, _ = make([[call("create_agent_workflow", name="X", purpose="post to slack"), done()]])
    config = cfg(thread="credgate")
    await drive(graph, user("build a slack agent"), config)
    state = await graph.aget_state(config)
    assert state.next == ("pause",)
    payload = state.tasks[0].interrupts[0].value
    assert payload["command"] == "request_credentials"
    assert payload["provider"] == "slack"


@pytest.mark.asyncio
async def test_build_flagged_agent_tells_user_human_review(monkeypatch: Any) -> None:
    _stub_build_and_vetting(monkeypatch, outcome="ready_for_review")
    graph, gw = make(
        [
            [call("create_agent_workflow", name="HR", purpose="answer HR questions"), done()],
            [txt("It needs a reviewer first."), done()],
        ]
    )
    config = cfg(thread="vet-flag")
    await drive(graph, user("build an HR agent"), config)

    tool_msgs = [m for m in gw.seen[1] if m.get("role") == "tool"]
    assert any("human reviewer" in m["content"] for m in tool_msgs)
    assert (await graph.aget_state(config)).next == ()
