"""Fan-out runtime tests — grows one slice at a time.

Slice 0: the `fanout` mode is wired (larger recursion backstop) and the TurnBudget
denial-of-wallet primitive behaves. No fan-out graph yet — fanout ≡ supervisor.
"""

from __future__ import annotations

import time
import uuid

from sila.adapters.auth.base import UserResult
from sila.core.config import settings
from sila.core.constants import Role
from sila.services.sila.orchestrator.budget import TurnBudget
from sila.services.sila.orchestrator.runner import GraphRunner


def _runner(*, fanout: bool) -> GraphRunner:
    return GraphRunner(
        db=None,
        gateway=object(),  # not used at construction
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        role=Role.BUILDER,
        workspace_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        emit=lambda _ev: None,
        fanout=fanout,
    )


# --- Slice 0: mode plumbing ------------------------------------------------


def test_fanout_mode_uses_the_larger_recursion_backstop() -> None:
    sup = _runner(fanout=False)
    fan = _runner(fanout=True)
    assert sup._config["recursion_limit"] == 24
    assert fan._config["recursion_limit"] == settings.sila_fanout_recursion_limit
    assert fan._config["recursion_limit"] > sup._config["recursion_limit"]
    # thread_id unchanged across modes (durability invariant).
    assert ":" in sup._config["configurable"]["thread_id"]


def test_fanout_flag_recorded() -> None:
    assert _runner(fanout=True)._fanout is True
    assert _runner(fanout=False)._fanout is False


# --- Slice 0: TurnBudget ---------------------------------------------------


def test_budget_token_cap_trips() -> None:
    b = TurnBudget(token_budget=100, deadline_s=999)
    b.charge(60)
    assert not b.exhausted
    b.charge(40)
    assert b.over_tokens and b.exhausted


def test_budget_ignores_nonpositive_charges() -> None:
    b = TurnBudget(token_budget=10, deadline_s=999)
    b.charge(0)
    b.charge(-5)  # type: ignore[arg-type]
    assert b.tokens_used == 0


def test_budget_time_cap_trips_and_start_clock_resets() -> None:
    b = TurnBudget(token_budget=10_000, deadline_s=-1)  # already past the deadline
    assert b.over_time and b.exhausted
    b2 = TurnBudget(token_budget=10_000, deadline_s=60)
    b2.charge(5)
    b2.start_clock()  # resets clock AND tokens
    assert b2.tokens_used == 0
    assert (time.monotonic() - b2._started) < 1.0


# --- Slice 1: tier serializer (the §3 deliverable) -------------------------

from sila.services.sila.orchestrator.runner import serialize_frame, tier_for  # noqa: E402


def test_expert_socket_receives_everything() -> None:
    ev = {"type": "tool", "name": "x", "status": "done", "expert": {"draft_id": "a1"}}
    assert serialize_frame(ev, "expert") is ev  # untouched


def test_beginner_drops_an_audience_expert_frame() -> None:
    ev = {"type": "plan", "audience": "expert", "subtasks": [{"id": "s1"}]}
    assert serialize_frame(ev, "beginner") is None


def test_beginner_strips_nested_expert_detail_keeps_friendly_fields() -> None:
    ev = {
        "type": "worker",
        "worker_id": "w1",
        "label": "Building the agent",
        "status": "done",
        "expert": {"draft_id": "a1", "n8n_workflow_id": "wf-9"},
    }
    out = serialize_frame(ev, "beginner")
    assert out == {
        "type": "worker",
        "worker_id": "w1",
        "label": "Building the agent",
        "status": "done",
    }
    assert "expert" not in out
    # The original is not mutated (a copy was returned).
    assert "expert" in ev


def test_beginner_passes_a_plain_frame_through_unchanged() -> None:
    ev = {"type": "token", "content": "hi"}
    assert serialize_frame(ev, "beginner") == ev


def test_tier_for_maps_roles() -> None:
    assert tier_for(Role.BUILDER) == "expert"
    assert tier_for(Role.ADMIN) == "expert"
    assert tier_for(Role.REVIEWER) == "expert"
    assert tier_for(Role.VIEWER) == "beginner"
    assert tier_for(Role.HIRER) == "beginner"


# --- Slice 2: planner classifier (cheap pre-filter) ------------------------

from sila.services.sila.orchestrator.planner import (  # noqa: E402
    COMPLEX,
    SIMPLE,
    classify_complexity,
)


def test_classify_defaults_simple() -> None:
    assert classify_complexity("what agents can answer IT tickets?") == SIMPLE
    assert classify_complexity("build an HR agent from my handbook") == SIMPLE  # one deliverable
    assert classify_complexity("") == SIMPLE


def test_classify_complex_needs_conjunction_plus_two_integrations() -> None:
    assert classify_complexity("build an agent and post summaries to slack and email") == COMPLEX
    assert classify_complexity("create a workflow then notify me on slack and via email") == COMPLEX


def test_classify_complex_on_two_builds_or_a_separate_second_agent() -> None:
    # "build X and build Y" — two builds, no integrations (the case the user hit).
    assert (
        classify_complexity(
            "build an HR policy agent from my handbook and build a separate IT agent"
        )
        == COMPLEX
    )
    assert classify_complexity("build an HR agent and build an IT agent") == COMPLEX
    # A single build + an explicit "separate/another" second-agent signal.
    assert (
        classify_complexity("build an agent from my docs and a separate finance agent") == COMPLEX
    )


def test_classify_is_conservative_one_integration_stays_simple() -> None:
    # A build + slack but no second integration → simple (don't over-fan-out).
    assert classify_complexity("build an agent that posts to slack") == SIMPLE


# --- Slice 2: build_fanout_graph (planner falls through to supervisor) ------

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from sila.adapters.llm.base import LLMDone, LLMTextDelta, LLMToolCall  # noqa: E402
from sila.services.sila.orchestrator.graph import GraphDeps, build_fanout_graph  # noqa: E402


class _Gw:
    """Minimal scripted gateway: yields the next round's chunks per stream_chat,
    and returns a scripted dict from complete_json (for the planner decomposition)."""

    def __init__(self, script: list[list], json_result: dict | None = None) -> None:
        self._s = list(script)
        self._json = json_result if json_result is not None else {}

    async def stream_chat(self, *, messages, tools, timeout_s: float = 60.0):  # noqa: ANN001
        chunks = self._s.pop(0) if self._s else [LLMDone()]
        for c in chunks:
            yield c

    async def complete_json(self, **_k):  # noqa: ANN003
        return self._json


class _Audit:
    _db = None

    async def record(self, **_k) -> None:  # noqa: ANN003
        return None


def _fanout_graph(script: list[list], json_result: dict | None = None):
    deps = GraphDeps(
        gateway=_Gw(script, json_result),  # type: ignore[arg-type]
        audit=_Audit(),  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        role=Role.BUILDER,
        workspace_id=uuid.uuid4(),
    )
    return build_fanout_graph(deps, InMemorySaver())


async def _run(graph, text: str) -> list:
    cfg = {"configurable": {"thread_id": "t"}, "recursion_limit": 48}
    frames: list = []
    async for ev in graph.astream(
        {"messages": [{"role": "user", "content": text}]}, cfg, stream_mode="custom"
    ):
        frames.append(ev)
    state = await graph.aget_state(cfg)
    return frames, state  # type: ignore[return-value]


async def test_fanout_simple_request_behaves_like_supervisor() -> None:
    graph = _fanout_graph([[LLMTextDelta(text="Hi there"), LLMDone()]])
    frames, state = await _run(graph, "hello there")
    tokens = "".join(f.get("content", "") for f in frames if f.get("type") == "token")
    assert tokens == "Hi there"
    assert not any(f.get("type") == "plan" for f in frames)  # simple → no plan frame
    assert not state.next  # reached END (not paused)


async def test_fanout_complex_without_decomposition_downgrades_to_simple() -> None:
    # complex heuristic but the decomposition (empty json) yields nothing → run as
    # the proven supervisor; no roster. The model answered a hard BUILD request in
    # prose, so the supervisor's deterministic build kick starts the interview (the
    # gate's docs step pauses the turn) — nothing depends on the model calling tools.
    graph = _fanout_graph([[LLMTextDelta(text="On it"), LLMDone()]])
    frames, state = await _run(graph, "build an agent and post to slack and email")
    assert not any(f.get("type") == "plan" for f in frames)
    tokens = "".join(f.get("content", "") for f in frames if f.get("type") == "token")
    assert tokens.startswith("On it")
    assert "starting the build" in tokens
    assert state.next == ("pause",)  # the gate's docs request paused the turn


async def test_fanout_still_pauses_on_a_pausing_tool() -> None:
    # Load-bearing: pause mechanism 1a survives the planner prefix. The agent emits
    # ask_user → graph pauses (state.next non-empty) exactly as under the supervisor.
    graph = _fanout_graph(
        [
            [
                LLMToolCall(
                    name="ask_user",
                    arguments={"questions": [{"question": "Which team?", "options": ["HR", "IT"]}]},
                )
            ]
        ]
    )
    _frames, state = await _run(graph, "set something up and ping slack and email")
    assert state.next  # paused at the interrupt


# --- Slice 3: planner LLM decomposition + roster ---------------------------

from sila.services.sila.orchestrator.planner import decompose  # noqa: E402


class _JsonGw:
    def __init__(self, result, raise_error: bool = False) -> None:  # noqa: ANN001
        self._result = result
        self._raise = raise_error

    async def complete_json(self, **_k):  # noqa: ANN003
        if self._raise:
            raise RuntimeError("boom")
        return self._result


async def test_decompose_caps_at_max_workers() -> None:
    gw = _JsonGw({"subtasks": [{"id": f"t{i}", "label": f"Task {i}"} for i in range(6)]})
    out = await decompose(gw, "…", max_workers=4)
    assert [s["id"] for s in out] == ["t0", "t1", "t2", "t3"]  # capped


async def test_decompose_dedupes_ids_and_drops_empty_labels() -> None:
    gw = _JsonGw(
        {
            "subtasks": [
                {"id": "a", "label": "First"},
                {"id": "a", "label": "Dup id"},  # same id → deduped
                {"label": "No id task"},  # gets a generated id
                {"id": "x", "label": ""},  # dropped — no label
            ]
        }
    )
    out = await decompose(gw, "…", max_workers=4)
    assert len(out) == 3  # the empty-label one dropped
    assert all(s["label"] for s in out)
    assert len({s["id"] for s in out}) == 3  # ids unique after dedup


async def test_decompose_falls_back_on_bad_or_error() -> None:
    assert await decompose(_JsonGw({}), "x", 4) == []
    assert await decompose(_JsonGw({"subtasks": "not-a-list"}), "x", 4) == []
    assert await decompose(_JsonGw(None, raise_error=True), "x", 4) == []


async def test_planner_complex_emits_roster_and_stores_plan() -> None:
    graph = _fanout_graph(
        [[LLMTextDelta(text="done"), LLMDone()]],
        json_result={
            "subtasks": [
                {"id": "agent", "label": "Build the agent"},
                {"id": "workflow", "label": "Build the Slack workflow"},
            ]
        },
    )
    frames, state = await _run(graph, "build an agent and post to slack and email")
    plan = [f for f in frames if f.get("type") == "plan"]
    queued = [f for f in frames if f.get("type") == "worker" and f.get("status") == "queued"]
    assert len(plan) == 1 and len(plan[0]["subtasks"]) == 2
    assert {w["worker_id"] for w in queued} == {"agent", "workflow"}
    assert all(w["audience"] == "expert" for w in queued)  # Beginner serializer drops these
    assert [s["id"] for s in state.values["plan"]] == ["agent", "workflow"]
    assert state.values["complexity"] == "complex"


async def test_planner_complex_but_single_subtask_downgrades_to_simple() -> None:
    graph = _fanout_graph(
        [[LLMTextDelta(text="done"), LLMDone()]],
        json_result={"subtasks": [{"id": "only", "label": "One thing"}]},
    )
    frames, state = await _run(graph, "build an agent and post to slack and email")
    assert not any(f.get("type") == "plan" for f in frames)  # no roster
    assert state.values["complexity"] == "simple"  # conservative downgrade
    assert state.values["plan"] == []


# --- Slice 4a: worker core (_run_subtask) — §C, retry, budget --------------

from sila.adapters.llm.base import LLMGatewayError  # noqa: E402
from sila.services.sila.orchestrator.graph import _run_subtask  # noqa: E402
from sila.services.sila.tool_registry import ToolContext  # noqa: E402


def _ctx() -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.BUILDER,
        defer_vetting=True,
    )


def _budget() -> TurnBudget:
    return TurnBudget(token_budget=10_000, deadline_s=60)


class _WorkerGw:
    """Scriptable worker gateway: a list of rounds, or an error toggle that clears
    after the first raise (to exercise the single transient retry)."""

    def __init__(self, rounds: list[list], *, errors: int = 0) -> None:
        self._rounds = list(rounds)
        self._errors = errors
        self.calls = 0

    async def stream_chat(self, *, messages, tools, timeout_s: float = 60.0):  # noqa: ANN001
        self.calls += 1
        if self._errors > 0:
            self._errors -= 1
            raise LLMGatewayError("transient")
        chunks = self._rounds.pop(0) if self._rounds else [LLMDone()]
        for c in chunks:
            yield c


async def test_worker_completes_with_a_text_result() -> None:
    gw = _WorkerGw([[LLMTextDelta(text="all set"), LLMDone()]])
    out = await _run_subtask(_ctx(), gw, _budget(), {"id": "w1", "label": "Do the thing"})
    assert out == {
        "id": "w1",
        "label": "Do the thing",
        "status": "done",
        "summary": "all set",
        "artifact_id": None,
    }


async def test_worker_fails_gracefully_on_a_pausing_ui_never_interrupts() -> None:
    # §C: when a tool returns a pausing UI (here request_documents → an open_modal,
    # a DB-free stand-in for the real connect_provider/upload pause), the worker must
    # FAIL gracefully — it must NEVER call interrupt() inside a Send branch.
    gw = _WorkerGw([[LLMToolCall(name="request_documents", arguments={"prompt": "docs"})]])
    out = await _run_subtask(_ctx(), gw, _budget(), {"id": "w1", "label": "Build from docs"})
    assert out["status"] == "failed"
    assert "needs more input" in out["reason"]


async def test_worker_retries_once_on_a_transient_error_then_succeeds() -> None:
    gw = _WorkerGw([[LLMTextDelta(text="recovered"), LLMDone()]], errors=1)
    out = await _run_subtask(_ctx(), gw, _budget(), {"id": "w1", "label": "x"})
    assert out["status"] == "done" and out["summary"] == "recovered"
    assert gw.calls == 2  # one failed call + one retry


async def test_worker_gives_up_after_a_second_error() -> None:
    gw = _WorkerGw([], errors=2)
    out = await _run_subtask(_ctx(), gw, _budget(), {"id": "w1", "label": "x"})
    assert out["status"] == "failed" and "unavailable" in out["reason"]


async def test_worker_fails_closed_when_budget_exhausted() -> None:
    spent = TurnBudget(token_budget=10, deadline_s=-1)  # already over time
    gw = _WorkerGw([[LLMTextDelta(text="should not run"), LLMDone()]])
    out = await _run_subtask(_ctx(), gw, spent, {"id": "w1", "label": "x"})
    assert out["status"] == "failed" and "budget" in out["reason"]
    assert gw.calls == 0  # never even called the model


# --- Slice 4a: full-graph fan-out + join + partial failure -----------------


async def test_fanout_runs_workers_then_joins_and_replies() -> None:
    # 2 workers (each completes with text), then join → agent composes a reply.
    # Script: worker round, worker round, agent reply round.
    graph = _fanout_graph(
        [
            [LLMTextDelta(text="built agent"), LLMDone()],
            [LLMTextDelta(text="built workflow"), LLMDone()],
            [LLMTextDelta(text="All done — both built."), LLMDone()],
        ],
        json_result={
            "subtasks": [
                {"id": "a", "label": "Build the agent"},
                {"id": "b", "label": "Build the workflow"},
            ]
        },
    )
    frames, state = await _run(graph, "build an agent and post to slack and email")
    done_lanes = [f for f in frames if f.get("type") == "worker" and f.get("status") == "done"]
    assert {w["worker_id"] for w in done_lanes} == {"a", "b"}  # both workers ran + finished
    results = state.values["worker_results"]
    assert len(results) == 2 and all(r["status"] == "done" for r in results)
    assert not state.next  # join → agent → END


async def test_fanout_partial_failure_one_worker_fails_others_proceed() -> None:
    # §A: one worker fails (a pausing UI → graceful fail), the other completes; the
    # turn does NOT fail — join reports both, agent replies. Order-independent: one
    # round is a pausing UI (fails), one is text (done), whichever worker gets which.
    graph = _fanout_graph(
        [
            [LLMToolCall(name="request_documents", arguments={"prompt": "docs"})],  # → fail
            [LLMTextDelta(text="built"), LLMDone()],  # → done
            [LLMTextDelta(text="One done, one needs input."), LLMDone()],  # agent reply
        ],
        json_result={
            "subtasks": [
                {"id": "a", "label": "Build the agent"},
                {"id": "b", "label": "Post to Slack"},
            ]
        },
    )
    frames, state = await _run(graph, "build an agent and post to slack and email")
    results = state.values["worker_results"]
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["done", "failed"]  # exactly one of each, whichever order
    failed_lanes = [f for f in frames if f.get("type") == "worker" and f.get("status") == "failed"]
    assert len(failed_lanes) == 1  # the failure shows as a failed lane, not a silent drop
    assert not state.next  # the turn still completes (partial success)


# --- Slice 4b: elicit connection pre-collection (§C, single-threaded) -------

from langgraph.types import Command  # noqa: E402

from sila.db.repositories.connectors import ConnectorBindingRepository  # noqa: E402


async def test_decompose_captures_integrations_lowercased() -> None:
    gw = _JsonGw({"subtasks": [{"id": "a", "label": "Post", "integrations": ["Slack", "Email"]}]})
    out = await decompose(gw, "x", 4)
    assert out[0]["integrations"] == ["slack", "email"]


def _slack_plan() -> dict:
    return {
        "subtasks": [
            {"id": "a", "label": "Build the agent", "integrations": []},
            {"id": "b", "label": "Post to Slack", "integrations": ["slack"]},
        ]
    }


def _two_workers_then_reply() -> list[list]:
    return [
        [LLMTextDelta(text="built agent"), LLMDone()],
        [LLMTextDelta(text="built workflow"), LLMDone()],
        [LLMTextDelta(text="All done."), LLMDone()],
    ]


async def test_elicit_pauses_to_connect_a_missing_oauth_app_before_any_worker(monkeypatch) -> None:  # noqa: ANN001
    async def _none(self, **_kw):  # noqa: ANN001, ANN003
        return None

    monkeypatch.setattr(ConnectorBindingRepository, "get", _none)
    graph = _fanout_graph(_two_workers_then_reply(), json_result=_slack_plan())
    frames, state = await _run(graph, "build an agent and post to slack and email")
    assert state.next  # paused in elicit, BEFORE fan-out
    iv = state.tasks[0].interrupts[0].value
    assert iv["command"] == "connect_provider" and iv["provider"] == "slack"
    # §C: no worker has started — elicitation is strictly single-threaded + first.
    assert not any(f.get("type") == "worker" and f.get("status") == "running" for f in frames)


async def test_elicit_proceeds_and_fans_out_when_already_connected(monkeypatch) -> None:  # noqa: ANN001
    async def _present(self, **_kw):  # noqa: ANN001, ANN003
        return object()

    monkeypatch.setattr(ConnectorBindingRepository, "get", _present)
    graph = _fanout_graph(_two_workers_then_reply(), json_result=_slack_plan())
    frames, state = await _run(graph, "build an agent and post to slack and email")
    assert not state.next  # no pause — already connected
    running = [f for f in frames if f.get("type") == "worker" and f.get("status") == "running"]
    assert len(running) == 2  # both workers ran after elicit cleared


async def test_elicit_re_runs_idempotently_after_connect_then_fans_out(monkeypatch) -> None:  # noqa: ANN001
    # §C idempotent re-run: missing on the first check (pause), present after the user
    # connects → resume re-runs elicit, skips the now-connected provider, fans out.
    state_box = {"n": 0}

    async def _get(self, **_kw):  # noqa: ANN001, ANN003
        state_box["n"] += 1
        return None if state_box["n"] == 1 else object()

    monkeypatch.setattr(ConnectorBindingRepository, "get", _get)
    graph = _fanout_graph(_two_workers_then_reply(), json_result=_slack_plan())
    cfg = {"configurable": {"thread_id": "elicit-resume"}, "recursion_limit": 48}
    user_msg = {
        "messages": [{"role": "user", "content": "build an agent and post to slack and email"}]
    }

    async for _ev in graph.astream(user_msg, cfg, stream_mode="custom"):
        pass
    assert (await graph.aget_state(cfg)).next  # paused for connect

    async for _ev in graph.astream(
        Command(resume="Slack is connected."), cfg, stream_mode="custom"
    ):
        pass
    s2 = await graph.aget_state(cfg)
    assert not s2.next  # resumed → elicit cleared → workers → join → reply → END
    assert len(s2.values["worker_results"]) == 2


# --- Slice 5: synthesizer + mandatory vetting funnel -----------------------

from types import SimpleNamespace  # noqa: E402


def _stub_fanout_build_and_vetting(monkeypatch, *, outcome: str) -> list[str]:  # noqa: ANN001
    """Workers that 'build' (return a vetted_agent_id, no n8n) + a scan_streamed that
    streams 4 checks and the given outcome (no DB). Returns the built agent ids."""
    import sila.services.sila.orchestrator.graph as gmod
    from sila.services.sila.tool_registry import ToolResult

    ids: list[str] = []

    async def fake_execute_tool(name: str, ctx, args: dict) -> ToolResult:  # noqa: ANN001
        if name == "create_agent_workflow":
            aid = str(uuid.uuid4())
            ids.append(aid)
            return ToolResult(
                content="Created draft agent.",
                summary="Created draft agent",
                meta={"vetted_agent_id": aid},
            )
        return ToolResult(content="ok")

    monkeypatch.setattr(gmod, "execute_tool", fake_execute_tool)

    class FakeAgentRepo:
        def __init__(self, _db) -> None: ...  # noqa: ANN001
        async def get(self, aid, wid):  # noqa: ANN001
            return SimpleNamespace(id=aid, workspace_id=wid)

    class FakeVetting:
        def __init__(self, _db) -> None: ...  # noqa: ANN001
        async def scan_streamed(self, *, agent, actor_id, on_step):  # noqa: ANN001
            for nm in ("pii", "prompt_injection", "llm_review", "workflow_rules"):
                on_step(nm, [])
            return SimpleNamespace(outcome=outcome)

    monkeypatch.setattr("sila.db.repositories.agent.AgentRepository", FakeAgentRepo)
    monkeypatch.setattr("sila.services.vetting_service.VettingService", FakeVetting)
    return ids


async def test_fanout_funnels_every_built_artifact_through_vetting(monkeypatch) -> None:  # noqa: ANN001
    ids = _stub_fanout_build_and_vetting(monkeypatch, outcome="auto_approved")
    # 2 workers each build (call create_agent_workflow → artifact), then synthesizer.
    graph = _fanout_graph(
        [
            [LLMToolCall(name="create_agent_workflow", arguments={"name": "A"})],
            [LLMToolCall(name="create_agent_workflow", arguments={"name": "B"})],
            [LLMTextDelta(text="Both agents are ready (Draft)."), LLMDone()],
        ],
        json_result={
            "subtasks": [
                {"id": "a", "label": "Build agent A", "integrations": []},
                {"id": "b", "label": "Build agent B", "integrations": []},
            ]
        },
    )
    frames, state = await _run(graph, "build an agent and post to slack and email")
    # Every built artifact funneled through the gate → two security_review reviews.
    reviews = [
        f
        for f in frames
        if f.get("type") == "tool"
        and f.get("name") == "security_review"
        and f.get("status") == "done"
    ]
    assert len(reviews) == 2
    summary = state.values["vetting_summary"]
    assert {v["id"] for v in summary} == set(ids)
    assert all(v["outcome"] == "auto_approved" for v in summary)
    # The synthesizer composed the reply (the only TTS-feeding node).
    assert "ready" in state.values["reply"].lower()
    assert not state.next


async def test_fanout_synthesizer_falls_back_when_llm_unavailable(monkeypatch) -> None:  # noqa: ANN001
    _stub_fanout_build_and_vetting(monkeypatch, outcome="ready_for_review")

    class _Boom(_Gw):
        async def stream_chat(self, *, messages, tools, timeout_s: float = 60.0):  # noqa: ANN001
            # workers still work; only the synthesizer (tools=[]) blows up.
            if tools == []:
                raise LLMGatewayError("down")
            for c in self._s.pop(0) if self._s else [LLMDone()]:
                yield c

    deps = GraphDeps(
        gateway=_Boom(
            [
                [LLMToolCall(name="create_agent_workflow", arguments={"name": "A"})],
                [LLMToolCall(name="create_agent_workflow", arguments={"name": "B"})],
            ],
            {
                "subtasks": [
                    {"id": "a", "label": "Build agent A", "integrations": []},
                    {"id": "b", "label": "Build agent B", "integrations": []},
                ]
            },
        ),  # type: ignore[arg-type]
        audit=_Audit(),  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        role=Role.BUILDER,
        workspace_id=uuid.uuid4(),
    )
    graph = build_fanout_graph(deps, InMemorySaver())
    _frames, state = await _run(graph, "build an agent and post to slack and email")
    # Deterministic fallback reply still produced; turn completes.
    assert "Draft" in state.values["reply"]
    assert not state.next


# --- Slice 6: durability across a WS drop + run_seed idempotency seed -------


def _build_deps(gw):  # noqa: ANN001
    return GraphDeps(
        gateway=gw,
        audit=_Audit(),  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        role=Role.BUILDER,
        workspace_id=uuid.uuid4(),
    )


async def test_fanout_survives_drop_at_elicit_and_builds_each_exactly_once(monkeypatch) -> None:  # noqa: ANN001
    # Durability: a fan-out paused at elicit (waiting on a connect) survives a WS drop
    # and resumes on a FRESH graph instance sharing the same checkpointer + thread_id.
    # And it builds each sub-task EXACTLY once across the drop (no duplicate side effect).
    ids = _stub_fanout_build_and_vetting(monkeypatch, outcome="auto_approved")
    box = {"n": 0}

    async def _get(self, **_kw):  # noqa: ANN001, ANN003
        box["n"] += 1
        return None if box["n"] == 1 else object()  # missing first (pause), then connected

    monkeypatch.setattr(ConnectorBindingRepository, "get", _get)

    gw = _Gw(
        [
            [LLMToolCall(name="create_agent_workflow", arguments={"name": "A"})],
            [LLMToolCall(name="create_agent_workflow", arguments={"name": "B"})],
            [LLMTextDelta(text="Both built."), LLMDone()],
        ],
        {
            "subtasks": [
                {"id": "a", "label": "Build agent", "integrations": []},
                {"id": "b", "label": "Post to Slack", "integrations": ["slack"]},
            ]
        },
    )
    deps = _build_deps(gw)  # shared across the "drop" (the gateway stays available)
    saver = InMemorySaver()
    cfg = {"configurable": {"thread_id": "durable-fanout"}, "recursion_limit": 48}
    user_msg = {
        "messages": [{"role": "user", "content": "build an agent and post to slack and email"}]
    }

    # Graph instance #1 → pauses at elicit (connect Slack). No build has happened yet.
    g1 = build_fanout_graph(deps, saver)
    async for _ev in g1.astream(user_msg, cfg, stream_mode="custom"):
        pass
    s1 = await g1.aget_state(cfg)
    assert s1.next  # paused in elicit, BEFORE any worker
    assert ids == []  # nothing built before the drop
    seed = s1.values["run_seed"]

    # "Reconnect": a brand-new graph instance, SAME checkpointer + thread_id.
    g2 = build_fanout_graph(deps, saver)
    async for _ev in g2.astream(Command(resume="Slack connected."), cfg, stream_mode="custom"):
        pass
    s2 = await g2.aget_state(cfg)
    assert not s2.next  # resumed → elicit cleared → fan out → vetting → reply → END
    assert len(ids) == 2  # each sub-task built EXACTLY once across the drop (no duplicate)
    assert s2.values["run_seed"] == seed  # stable idempotency seed survived the reconnect
    assert len(s2.values["vetting_summary"]) == 2  # both funneled through the gate
