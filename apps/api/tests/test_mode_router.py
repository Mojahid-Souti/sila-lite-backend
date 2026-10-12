"""AUTO intent router: deterministic hard-routing + code-level tool-scope enforcement."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sila.adapters.auth.base import UserResult
from sila.adapters.llm.base import LLMChunk, LLMDone, LLMTextDelta, LLMToolCall
from sila.core.constants import Role
from sila.services.sila.concierge_service import (
    _AUTO_PROMPT,
    _PLAN_PROMPT_AUTO,
    _SYSTEM_PROMPT,
    PLAN_TOOL_NAMES,
)
from sila.services.sila.mode_router import (
    INTENT_BUILD,
    INTENT_PLAN,
    resolve_auto,
    route_intent,
)
from sila.services.sila.orchestrator.graph import GraphDeps, build_supervisor_graph

# ---------------------------------------------------------------------------
# route_intent — the deterministic signals
# ---------------------------------------------------------------------------

BUILD_TEXTS = [
    "build me an agent that answers HR questions from my handbook",
    "Create a workflow that posts updates to the team",
    "can you build an agent for expense reports?",
    "set up an agent to summarize documents",
    "I want to build an agent that tracks tickets",
    'Build an agent called "Daily Doc Digest" that summarizes my docs',
    "ok build it",
    "yes, go ahead and build it",
    "draft a workflow that collects weekly numbers",
    # No build verb, but a destination + recurring publish intent = an automation.
    "post a summary of my documents to slack every morning at 9",
    "email me a digest of new policies daily",
    # Orchestration phrasing is a build request too (the glm4 prose-essay case).
    "I need you to orchestrate a competitive analysis and outreach workflow for sales",
    "please execute the following multi-step plan",
    "automate this pipeline for me",
]

PLAN_TEXTS = [
    "is there an agent that can translate documents?",
    "find me an agent that can summarize meeting notes",
    "what can you do?",
    "help me figure out what kind of agent I need",
    "help me think through automating our HR answers",
    "do you have any agents for onboarding?",
    "can you recommend an agent for compliance checks?",
    "is there a ready-made agent for this already?",
]

AMBIGUOUS_TEXTS = [
    # Analysis/consultation — must NOT hard-route to build (live bug: a compare
    # question force-started a build past the model's own "shall I proceed?"),
    # and not to Plan either (Plan turned comparisons into full plan proposals).
    "Compare Slack, Teams, and Gmail as destinations for a daily digest agent",
    "think through the trade-offs of posting daily summaries to slack",
    "what are the pros and cons of emailing a digest every morning?",
    "summarize this document: quarterly results were strong",
    "translate the following to English",
    "hello",
    "what's the difference between a KB and a connector?",
    "can you check my grammar?",
    "",
]


@pytest.mark.parametrize("text", BUILD_TEXTS)
def test_build_signals_hard_route(text: str) -> None:
    assert route_intent(text) == INTENT_BUILD


@pytest.mark.parametrize("text", PLAN_TEXTS)
def test_plan_signals_hard_route(text: str) -> None:
    assert route_intent(text) == INTENT_PLAN


@pytest.mark.parametrize("text", AMBIGUOUS_TEXTS)
def test_ambiguous_goes_to_model(text: str) -> None:
    assert route_intent(text) is None


def test_exploration_about_agents_beats_destination_heuristic() -> None:
    # Asking ABOUT an agent that posts to Slack is discovery, not a build order.
    assert route_intent("is there an agent that posts daily summaries to slack?") == INTENT_PLAN


# ---------------------------------------------------------------------------
# resolve_auto — behaviour mapping + sticky build
# ---------------------------------------------------------------------------


def test_resolve_build_gets_the_builder_without_discovery() -> None:
    """Build keeps every building tool but NOT search_bazaar/hire_agent. Used to
    be `tools is None` (everything), which is how a build request opened with
    hire_agent and ended up recommending an unrelated agent."""
    prompt, tools = resolve_auto("build me an agent that answers HR questions", {})
    assert prompt == _SYSTEM_PROMPT
    assert tools is not None
    assert "create_agent_workflow" in tools and "ask_user" in tools
    assert "search_bazaar" not in tools and "hire_agent" not in tools


def test_resolve_plan_gets_advisory_scope() -> None:
    prompt, tools = resolve_auto("what can you do?", {})
    assert prompt == _PLAN_PROMPT_AUTO
    assert tools == PLAN_TOOL_NAMES
    assert "create_agent_workflow" not in (tools or set())


def test_resolve_ambiguous_gets_auto_prompt_all_tools() -> None:
    prompt, tools = resolve_auto("summarize this document", {})
    assert prompt == _AUTO_PROMPT
    assert tools is None


def test_active_build_interview_is_sticky() -> None:
    # Mid-interview resumes ("Every weekday at 9. Please continue.") must stay build,
    # whatever the text looks like — even pure chit-chat.
    build_state = {"build_args": {"name": "Digest", "purpose": "daily summary"}}
    prompt, tools = resolve_auto("thanks!", build_state)
    assert prompt == _SYSTEM_PROMPT
    assert tools is not None
    assert "search_bazaar" not in tools and "hire_agent" not in tools


def test_mid_conversation_flip_plan_to_build() -> None:
    # A planning chat that ends in "ok build it" flips to build on the next message.
    assert route_intent("help me figure out what agent I need") == INTENT_PLAN
    assert route_intent("ok build it") == INTENT_BUILD


# ---------------------------------------------------------------------------
# Graph enforcement — a blocked build tool NEVER executes in a restricted scope
# ---------------------------------------------------------------------------


class _BuildAttemptGateway:
    """Round 1: tries create_agent_workflow (out of scope). Round 2: recovers in text."""

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
            # The restricted schema was offered, but a weak model hallucinates anyway.
            yield LLMToolCall(
                name="create_agent_workflow",
                arguments={"name": "Sneaky", "purpose": "should never run"},
            )
            yield LLMDone()
        else:
            yield LLMTextDelta(text="I can't build here — but here's the plan.")
            yield LLMDone()

    async def complete_json(self, *, system_prompt: str, user_prompt: str, timeout_s: float = 30.0):
        return {}


class _FakeAudit:
    _db = None

    async def record(self, **kwargs: Any) -> None:  # noqa: ANN401
        return None


@pytest.mark.asyncio
async def test_restricted_scope_blocks_build_tool_in_code() -> None:
    """Plan scope (fixed or AUTO-routed): the build tool is dropped BEFORE execution —
    execute_tool is never reached (it would explode on the None db), and the model
    recovers with a plain-text answer on the retry round."""
    gateway = _BuildAttemptGateway()
    deps = GraphDeps(
        gateway=gateway,  # type: ignore[arg-type]
        audit=_FakeAudit(),  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        role=Role.BUILDER,
        workspace_id=uuid.uuid4(),
        tool_names=PLAN_TOOL_NAMES,
    )
    graph = build_supervisor_graph(deps, InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}}
    out = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "build me an agent"}]}, config
    )
    assert gateway.rounds == 2  # blocked → retry → recovered
    assert "can't build here" in out["reply"]


@pytest.mark.asyncio
async def test_auto_mode_routes_plan_and_blocks_build() -> None:
    """AUTO + an exploration message routes to plan behaviour, whose scope blocks the
    build tool the same way (routing gates what is USED, not what exists)."""
    gateway = _BuildAttemptGateway()
    deps = GraphDeps(
        gateway=gateway,  # type: ignore[arg-type]
        audit=_FakeAudit(),  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        role=Role.BUILDER,
        workspace_id=uuid.uuid4(),
        mode="auto",
    )
    graph = build_supervisor_graph(deps, InMemorySaver())
    config = {"configurable": {"thread_id": "t2"}}
    out = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "what can you do?"}]}, config
    )
    assert gateway.rounds == 2
    assert "can't build here" in out["reply"]


# ---------------------------------------------------------------------------
# Attached-file questions ANSWER — they never build, and content never routes
# ---------------------------------------------------------------------------


def test_attached_file_question_routes_to_answer_with_zero_tools() -> None:
    from sila.services.sila.concierge_service import _ANSWER_PROMPT

    msg = (
        "كم عدد أيام الإجازة السنوية للموظف؟\n\n"
        "[Attached file: الاجازة.txt]\nسياسة الموارد البشرية...\n[End of الاجازة.txt]"
    )
    prompt, tools = resolve_auto(msg, {})
    assert prompt == _ANSWER_PROMPT
    assert tools == frozenset()  # no tools — it can only answer


def test_attachment_content_never_steers_the_route() -> None:
    # The FILE mentions workflows; the USER asked a question. Words rule, not data.
    msg = (
        "what does this say?\n\n"
        "[Attached file: notes.txt]\nbuild an agent workflow pipeline\n[End of notes.txt]"
    )
    from sila.services.sila.mode_router import split_attachments

    words, has = split_attachments(msg)
    assert has and words == "what does this say?"
    assert route_intent(words) is None  # ambiguous words → answer route via attachment


def test_build_request_with_attachment_still_builds() -> None:
    msg = "build an agent from this handbook\n\n[Attached file: hr.txt]\ncontent\n[End of hr.txt]"
    prompt, tools = resolve_auto(msg, {})
    assert prompt == _SYSTEM_PROMPT
    assert tools is not None
    assert "search_bazaar" not in tools and "hire_agent" not in tools


# ---------------------------------------------------------------------------
# wants_data_dashboard — the data-dashboard kick's word matcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "create for me a grafana dashbord to see some insights"[:35] + " insights",
        "build a dashboard from this",
        "chart this data",
        "visualize the sales file",
        "give me some insights",
        "plot revenue by region",
    ],
)
def test_wants_data_dashboard_matches(text: str) -> None:
    from sila.services.sila.mode_router import wants_data_dashboard

    assert wants_data_dashboard(text)


@pytest.mark.parametrize(
    "text",
    ["summarize this document", "build an agent from my handbook", "hello", ""],
)
def test_wants_data_dashboard_ignores(text: str) -> None:
    from sila.services.sila.mode_router import wants_data_dashboard

    assert not wants_data_dashboard(text)


def test_attachment_with_chart_intent_keeps_tools() -> None:
    """LIVE BUG: CSV + "create a dashboard for insights" fell into the answer-only
    route (zero tools) — the model analyzed the file in prose and no dashboard
    could fire. Chart intent over an attachment keeps the full toolset."""
    from sila.services.sila.mode_router import resolve_auto

    attach = "\n\n[Attached file: f.csv]\na,b\n1,2\n[End of f.csv]"
    msg = f"create for me a dashboard to see some insights{attach}"
    _prompt, tools = resolve_auto(msg, {})
    assert tools is None  # full toolset — NOT the answer-only frozenset()

    plain = f"summarize this{attach}"
    _prompt2, tools2 = resolve_auto(plain, {})
    assert tools2 == frozenset()  # non-chart attachment questions stay answer-only

    # Chart words INSIDE the file must not trigger (only the user's words count).
    sneaky = "summarize this\n\n[Attached file: f.csv]\ndashboard,insights\n1,2\n[End of f.csv]"
    _prompt3, tools3 = resolve_auto(sneaky, {})
    assert tools3 == frozenset()
