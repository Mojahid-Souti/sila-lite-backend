"""BUILD turns must not be able to hire or search.

The reported failure: "I want to build an agent that analyze investment
opportunities" opened with hire_agent, invented an id, recovered into a search,
and then offered to hire an unrelated "Daily Education Summary". The system
prompt already said "never search when asked to BUILD" — a 14B model picking
among 39 tools did not obey it. These tests pin the structural guard instead:
on a build turn the discovery tools are not offered at all.
"""

from pathlib import Path

import pytest

from sila.services.sila.mode_router import INTENT_BUILD, INTENT_PLAN, resolve_auto, route_intent

DISCOVERY = ("search_bazaar", "hire_agent")

BUILD_PHRASINGS = [
    "I want to build an agent that analyze investment opportunities",
    "build me an agent that analyzes investment opportunities",
    "create an agent for investment analysis",
    "make me a bot that summarises our inbox",
    # No build VERB at all — these used to fall through to AUTO with every tool.
    "I need an agent to analyze investments",
    "I want an agent that watches our inbox",
]

FIND_PHRASINGS = [
    "find me an agent that analyzes investments",
    "is there an agent for investments?",
    "do you have any agents for reporting?",
]


@pytest.mark.parametrize("text", BUILD_PHRASINGS)
def test_build_requests_route_to_build(text: str) -> None:
    assert route_intent(text) == INTENT_BUILD


@pytest.mark.parametrize("text", BUILD_PHRASINGS)
def test_build_turns_withhold_the_discovery_tools(text: str) -> None:
    """The whole fix: the model cannot choose hire_agent on a build turn,
    because hire_agent is not in the toolset for that turn."""
    _prompt, tools = resolve_auto(text, {})
    assert tools is not None, "build must restrict the toolset, not allow everything"
    for name in DISCOVERY:
        assert name not in tools, f"{name} must not be offered on a build turn"


@pytest.mark.parametrize("text", BUILD_PHRASINGS)
def test_build_keeps_the_tools_it_actually_needs(text: str) -> None:
    """A denylist, not an allowlist — restricting must not quietly strip the
    build interview's own tools."""
    _prompt, tools = resolve_auto(text, {})
    assert tools is not None
    for name in ("create_agent_workflow", "ask_user", "request_documents"):
        assert name in tools, f"{name} is required by the build flow"


def test_mid_interview_stays_restricted() -> None:
    """A build in flight is sticky: slot answers must not re-open hiring."""
    _prompt, tools = resolve_auto("every monday", {"build_args": {"goal": "x"}})
    assert tools is not None
    assert all(n not in tools for n in DISCOVERY)


@pytest.mark.parametrize("text", FIND_PHRASINGS)
def test_find_requests_keep_discovery(text: str) -> None:
    """The guard must not break the case discovery exists for."""
    assert route_intent(text) == INTENT_PLAN
    _prompt, tools = resolve_auto(text, {})
    assert tools is not None
    assert all(n in tools for n in DISCOVERY)


@pytest.mark.asyncio
async def test_blank_search_does_not_return_the_catalogue() -> None:
    """search_catalog with an empty query is a browse-all. Presenting that as
    "Top matches" is how an investment request was answered with "Daily
    Education Summary" — the only published agent, matched against nothing."""
    import uuid
    from unittest.mock import AsyncMock

    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext, execute_tool

    ctx = ToolContext(
        db=AsyncMock(),
        user=UserResult(id=uuid.uuid4(), email="u@x.io", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.ADMIN,
    )
    ctx.build_state = {}
    for args in ({}, {"query": ""}, {"query": "  "}):
        res = await execute_tool("search_bazaar", ctx, args)
        assert res.summary == "No search terms"
        assert "never present catalogue entries" in res.content.lower()
    # The DB must never have been touched for a query that was never run.
    ctx.db.execute.assert_not_awaited()


def test_identical_tool_calls_in_one_round_collapse() -> None:
    """A weak model emitting the same call twice ran it twice — the user watched
    "Searching the Bazaar / No matches found" duplicated back to back. Identity
    is name + arguments, so two DIFFERENT searches still both run."""
    from sila.services.sila.orchestrator.graph import _dedupe_calls

    seen: set[str] = set()
    calls = [
        {"name": "search_bazaar", "arguments": {"query": "finance"}},
        {"name": "search_bazaar", "arguments": {"query": "finance"}},
        {"name": "search_bazaar", "arguments": {"query": "budget"}},
    ]
    out = _dedupe_calls(calls, seen)
    assert [c["arguments"]["query"] for c in out] == ["finance", "budget"]


def test_dedupe_key_ignores_argument_ordering() -> None:
    """Same arguments in a different key order is the same call."""
    from sila.services.sila.orchestrator.graph import _dedupe_calls

    seen: set[str] = set()
    out = _dedupe_calls(
        [
            {"name": "t", "arguments": {"a": 1, "b": 2}},
            {"name": "t", "arguments": {"b": 2, "a": 1}},
        ],
        seen,
    )
    assert len(out) == 1


def test_bazaar_no_match_tells_the_model_not_to_search_again() -> None:
    """Without this the model re-ran the identical search next round."""
    from sila.services.sila.tool_registry import TOOLS

    assert "search_bazaar" in TOOLS  # guards the rename case
    src = Path("sila/services/sila/tool_registry.py").read_text()
    assert "do\nNOT search again" in src or "NOT search again" in src


@pytest.mark.parametrize(
    ("purpose", "content", "expected"),
    [
        # Web-sourced publishers must NOT be asked to upload anything — asking
        # opened a panel the user had nothing to put in, and read as the
        # assistant not having listened to the brief.
        ("see the latest news from the web and post a summary to slack", "", False),
        ("summarise AI news headlines each morning", "", False),
        ("post market data and press releases daily", "", False),
        ("daily brief", "The latest news from the web on a topic", False),
        # Document-sourced publishers still need them.
        ("post a daily digest of our handbook", "", True),
        ("daily brief", "A summary of only newly-added documents", True),
    ],
)
def test_only_document_sourced_publishers_are_asked_for_uploads(
    purpose: str, content: str, expected: bool
) -> None:
    from sila.services.sila import build_slots as bs

    assert bs.needs_documents(bs.SCHEDULED_PUBLISHER, purpose, content) is expected


def test_a_knowledge_agent_always_needs_documents() -> None:
    """It answers FROM the user's files — that is the whole agent."""
    from sila.services.sila import build_slots as bs

    assert bs.needs_documents(bs.KNOWLEDGE, "answer HR questions from the web", "") is True


def test_web_sourced_is_an_offered_content_option() -> None:
    """The options were all document-centric, so a web brief had to pick a
    document answer — which then did nothing."""
    from sila.services.sila import build_slots as bs

    content = next(s for s in bs.REQUIRED_SLOTS[bs.SCHEDULED_PUBLISHER] if s.name == "content")
    assert any("web" in o.lower() for o in content.options)


def test_the_plan_recap_carries_the_spec_the_model_must_remember() -> None:
    """propose_plan used to hand the model "Proposed the plan to the user as a
    card" and nothing else, so the next turn it answered "I don't have a plan on
    record in this chat" about a plan it had just written itself."""
    from sila.services.sila.tool_registry import _plan_recap

    recap = _plan_recap(
        {
            "goal": "Post an AI news summary to Slack each weekday",
            "content": "3-5 one-line headlines",
            "sources": "the public web",
            "schedule": "Every weekday at 8:00 AM",
            "destination": "#general",
            "integrations": ["slack"],
            "steps": [{"title": "Search"}, {"title": "Summarise"}, {"title": "Post"}],
        }
    )
    for expected in ("AI news summary", "8:00 AM", "#general", "slack", "public web"):
        assert expected in recap, expected


def test_the_plan_recap_survives_an_empty_plan() -> None:
    from sila.services.sila.tool_registry import _plan_recap

    assert _plan_recap({})  # never an empty string handed to the model


def test_propose_plan_tells_the_model_to_honour_a_typed_approval() -> None:
    """The card's buttons are one way to accept; "go ahead" in the user's own
    words is another, and it used to send the model back to square one."""
    src = Path("sila/services/sila/tool_registry.py").read_text()
    body = src[src.index("async def _propose_plan") :][:4000]
    assert "approve in their own words" in body
    assert "do not ask them to specify it again" in body


def test_artifact_shape_is_uniform_across_kinds() -> None:
    """Deck, document, sheet, agent — one shape, so the UI renders them as one
    family instead of each tool inventing its own payload."""
    from sila.services.sila.tool_registry import _artifact

    deck = _artifact("deck", "Q1 Review", url="https://x/1", subtitle="PowerPoint · 9 slides")
    assert deck == {
        "kind": "deck",
        "name": "Q1 Review",
        "url": "https://x/1",
        "subtitle": "PowerPoint · 9 slides",
    }
    agent = _artifact("agent", "AI News Brief", id="abc", integrations=["slack"], web=True)
    assert agent["kind"] == "agent"
    assert agent["url"] is None  # an agent routes internally, it is not a file
    assert agent["integrations"] == ["slack"] and agent["web"] is True


def test_produced_files_travel_as_data_not_only_prose() -> None:
    """The deck and document used to exist only as a markdown link the model was
    asked to repeat, so whether the user ever saw them depended on the model."""
    import re as _re

    src = Path("sila/services/sila/tool_registry.py").read_text()
    kinds = _re.findall(r'_artifact\(\s*"([a-z]+)"', src)
    assert kinds.count("deck") >= 2, "both the download and the OneDrive path"
    assert kinds.count("doc") >= 1
    assert kinds.count("agent") >= 2, "the deferred-vetting and loop paths"


@pytest.mark.skipif(
    not Path("../web/components/app/sila/sila-trace.tsx").is_file(),
    reason="the website is not in this checkout (the backend ships on its own)",
)
def test_the_frontend_knows_every_kind_the_backend_emits() -> None:
    """A kind with no icon entry would fall back silently and read as the wrong
    thing, so the two lists must not drift."""
    import re as _re

    src = Path("sila/services/sila/tool_registry.py").read_text()
    emitted = set(_re.findall(r'_artifact\(\s*"([a-z]+)"', src))
    trace = Path("../web/components/app/sila/sila-trace.tsx").read_text()
    # Read the UI's ARTIFACT_META keys rather than listing them here — an
    # enumerated list has to be edited every time a kind is added, which makes
    # the failure look like the test's problem instead of the drift's.
    block = trace.split("ARTIFACT_META", 1)[1].split("};", 1)[0]
    known = set(_re.findall(r"^\s+([a-z]+):\s*\{", block, _re.M))
    assert known, "could not read the UI's artifact kinds"
    assert emitted <= known, f"backend emits kinds the UI cannot render: {emitted - known}"


def test_no_tool_asks_the_model_to_paste_a_download_link() -> None:
    """Produced files render as attachment tiles. A tool that ALSO tells the
    model to print "[download the deck](url)" puts a second, louder copy of the
    same affordance mid-sentence — which is what the tile replaced."""
    import re as _re

    src = Path("sila/services/sila/tool_registry.py").read_text()
    # Ignore the module's own docstrings, which quote the old wording as history.
    instructions = [
        line
        for line in src.splitlines()
        if _re.search(r"\[download the \w+\]\(url\)", line) and "which made whether" not in line
    ]
    assert not instructions, f"tool still asks for an inline download link: {instructions}"


def test_every_download_path_attaches_its_file() -> None:
    """Each place that mints a 24h capability URL must also emit an artifact, or
    the file exists and the user never sees it."""
    import re as _re

    src = Path("sila/services/sila/tool_registry.py").read_text()
    minted = src.count("/v1/files/{row.id}")
    kinds = _re.findall(r'_artifact\(\s*"([a-z]+)"', src)
    file_artifacts = sum(kinds.count(k) for k in ("doc", "deck", "sheet", "pdf"))
    assert (
        file_artifacts >= minted
    ), f"{minted} download URLs minted but only {file_artifacts} artifacts emitted"


def test_both_tool_paths_share_one_done_frame_builder() -> None:
    """The pausing path had its own frame literal and dropped citations and
    artifacts, so a tool that both pauses and produces a file lost it."""
    src = Path("sila/services/sila/orchestrator/graph.py").read_text()
    assert src.count("tool_done_frame(") >= 3, "one definition plus both call sites"


def test_a_document_can_return_a_pdf_from_one_call() -> None:
    """Asking for "a PDF and a Word doc" took two prompts because the registry
    had no PDF path at all — it could only produce Office files."""
    from sila.services.sila.tool_registry import TOOLS

    tool = TOOLS["create_docx"]
    assert "also_pdf" in tool.parameters["properties"]
    assert "pdf" in tool.description.lower()


@pytest.mark.asyncio
async def test_a_failed_pdf_conversion_never_costs_the_document(monkeypatch) -> None:
    """The PDF is a second rendering of a document that already exists and is
    already attached. Gotenberg being down must cost the PDF, not the doc."""
    import uuid as _uuid
    from unittest.mock import AsyncMock

    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila import tool_registry as tr

    async def down(_data, _filename):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("sila.services.office_convert.office_to_pdf", down)
    ctx = tr.ToolContext(
        db=AsyncMock(),
        user=UserResult(id=_uuid.uuid4(), email="u@x.io", email_confirmed=True),
        workspace_id=_uuid.uuid4(),
        role=Role.ADMIN,
    )
    ctx.build_state = {}
    assert await tr._pdf_sibling(ctx, b"x", "r.docx", "R", wanted=True) == []
    # And not asked for at all → no conversion attempted.
    assert await tr._pdf_sibling(ctx, b"x", "r.docx", "R", wanted=False) == []


def test_the_prompt_requires_every_requested_format_in_one_turn() -> None:
    from sila.services.sila.concierge_service import _SYSTEM_PROMPT

    assert "MULTIPLE OUTPUTS" in _SYSTEM_PROMPT
    assert "same turn" in _SYSTEM_PROMPT
