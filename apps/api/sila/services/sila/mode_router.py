"""AUTO-mode intent router — deterministic per-message behavior selection.

AUTO must route each message to the right behaviour (build / plan / converse)
without the user picking a mode label. A weak model misrouting "build me X"
into chat is a failure, so STRONG signals hard-route in code — only genuinely
ambiguous messages are left to the model's judgment (the combined AUTO prompt).

Routing rules, in precedence order (first match wins):
  1. An active build interview (build_args stashed) → BUILD, whatever the text
     says — answers/uploads mid-interview must not derail the gate.
  2. Explicit exploration ("is there an agent…", "find me an agent",
     "what can you do", "help me figure out…") → PLAN behaviour: the advisory
     prompt + the restricted discovery toolset (cannot build until the user
     says to).
  3. Explicit build verbs ("build/create/make/set up … agent/workflow", or a
     bare "build it" confirmation) → BUILD behaviour: the full builder prompt
     + all tools.
  4. A wired destination + a recurring publish intent ("post … to Slack every
     morning") → BUILD (it's a standing automation, even without the word
     "build").
  5. Anything else → the model decides (combined AUTO prompt, all tools).

Because routing re-runs per message, AUTO switches behaviour mid-conversation:
a planning chat that ends in "ok build it" flips to the build interview on the
next message without a mode change.

Complexity: O(len(text)) per message — a fixed list of compiled regexes.
"""

from __future__ import annotations

import re
from typing import Any

from sila.services.sila.concierge_service import (
    _ANSWER_PROMPT,
    _AUTO_PROMPT,
    _PLAN_PROMPT_AUTO,
    PLAN_TOOL_NAMES,
    prompt_for_mode,
)

INTENT_BUILD = "build"
INTENT_PLAN = "plan"

# Discovery tools, withheld on a BUILD turn. The user said BUILD; offering the
# model a way to hire instead is offering it a way to be wrong.
#
# The prompt already said "never search when asked to BUILD" and the model did
# it anyway — it opened with hire_agent on "I want to build an agent that
# analyzes investment opportunities", invented an id, recovered into a search,
# and then offered to hire an unrelated "Daily Education Summary". A 14B model
# choosing among 39 tools does not reliably obey a sentence. Removing the tools
# from the turn makes the wrong move unrepresentable instead of discouraged.
_DISCOVERY_TOOL_NAMES = frozenset({"search_bazaar", "hire_agent"})


def _build_tool_names() -> frozenset[str]:
    """Everything except discovery. A denylist, not an allowlist: the build
    interview reaches for connectors, uploads and schedule helpers as it goes,
    and an allowlist would quietly break whichever one nobody listed."""
    from sila.services.sila.tool_registry import TOOLS

    return frozenset(TOOLS) - _DISCOVERY_TOOL_NAMES


# The composer embeds attached-file content behind this marker. Routing (and the
# pinned build goal) must look only at the USER'S OWN WORDS before it — file
# content full of words like "workflow" must never steer the route.
ATTACH_MARKER = "[Attached file:"


def split_attachments(text: str) -> tuple[str, bool]:
    """(the user's words without any attached-file blocks, had_attachments)."""
    t = text or ""
    if ATTACH_MARKER in t:
        return t.split(ATTACH_MARKER, 1)[0].strip(), True
    return t, False


# Exploration / discovery — checked FIRST so "is there an agent that posts to
# Slack?" stays a question, not a build. Kept narrow: a bare "can you …" is NOT
# a plan signal (it's how people phrase build requests too) — that's ambiguous.
_PLAN_SIGNALS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bis there (an?|any)\b.{0,60}\b(agent|bot|workflow)\b",
        r"\b(find|show|get) (me )?(an? |some )?(agents?|bots?)\b",
        r"\bwhat can you do\b",
        r"\bhelp me (figure|think|decide|choose|plan|understand|work) ",
        r"\b(which|what) (agent|kind of agent)\b.{0,40}\b(should|would|do i|fits|need)\b",
        r"\bdo you have\b.{0,60}\b(agents?|bots?)\b",
        r"\brecommend\b.{0,50}\b(agents?|bots?)\b",
        r"\b(existing|ready[- ]?made|already[- ]?built|off[- ]the[- ]shelf)\b"
        r".{0,40}\b(agents?|bots?)\b",
    )
)

# Explicit creation intent — the strongest signal. Includes the bare "build it"
# confirmation so a planning chat can flip to build on the user's word.
_BUILD_SIGNALS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(build|create|make|set\s?up|draft)\b.{0,70}\b(agent|workflow|bot|assistant|automation)\b",
        r"\b(orchestrate|automate|implement|execute)\b.{0,90}\b(workflow|pipeline|automation|plan)\b",
        r"\bbuild\s+(it|this|that|one|him|her)\b",
        r"\b(go ahead|proceed|yes)\b.{0,25}\b(build|building)\b",
        # "I need an agent to analyse investments" — a build request with no
        # build VERB, which previously fell through to AUTO with every tool
        # available. Safe to route here because the find/explore phrasings
        # ("find me an agent", "is there an agent") are PLAN signals and PLAN
        # is matched first.
        r"\bi (need|want)\b.{0,30}\b(agent|bot|assistant|automation)\b"
        r".{0,12}\b(that|to|which|for)\b",
    )
)

# A wired delivery destination + a recurring publish intent is a build request
# even without a build verb ("post my doc summaries to Slack every morning").
_DESTINATION = re.compile(r"\b(slack|teams|discord|telegram|e-?mail)\b", re.IGNORECASE)
_PUBLISH_VERB = re.compile(
    r"\b(post|send|publish|notify|alert|remind|summari[sz]e|digest)\b", re.IGNORECASE
)
_CADENCE = re.compile(
    r"\b(every|daily|weekly|hourly|each\s+(day|morning|week)|schedule[ds]?|at\s+\d)\b",
    re.IGNORECASE,
)


# Analysis / consultation — "compare X and Y…", "think through the trade-offs".
# Suppresses the BUILD hard-route (live bug: "Compare Slack, Teams and Gmail …
# daily digest agent" force-started a build past the model's own "shall I
# proceed?" question) but does NOT force Plan mode either — the model answers
# the question directly (Plan mode turned comparisons into full plan proposals).
_ANALYSIS = re.compile(r"\bcompare\b|\btrade-?offs?\b|\bpros\s+and\s+cons\b", re.IGNORECASE)


# Chart/dashboard intent over an ATTACHED data file — drives the deterministic
# data-dashboard kick (live bug: CSV + "create a grafana dashboard for insights"
# got a prose lecture instead of a built dashboard).
# dashb\w* absorbs the common typos ("dashbord", "dashbaord").
_CHART_WORDS = re.compile(
    r"\b(dashb\w*|charts?|graphs?|plots?|insights?|visuali[sz]\w*)\b",
    re.IGNORECASE,
)


def wants_data_dashboard(words: str) -> bool:
    """True when the user's OWN WORDS ask for charts/insights (pair with an
    attached data file before kicking). O(len(words))."""
    return bool(_CHART_WORDS.search(words or ""))


def route_intent(text: str) -> str | None:
    """Classify one message: INTENT_PLAN | INTENT_BUILD | None (model decides).

    Plan signals are checked before build so exploration ABOUT agents ("is
    there an agent that…") never triggers a build. O(len(text))."""
    t = (text or "").strip()
    if not t:
        return None
    if any(p.search(t) for p in _PLAN_SIGNALS):
        return INTENT_PLAN
    if _ANALYSIS.search(t):
        return None  # a question to answer — never a hard route
    if any(p.search(t) for p in _BUILD_SIGNALS):
        return INTENT_BUILD
    if _DESTINATION.search(t) and _PUBLISH_VERB.search(t) and _CADENCE.search(t):
        return INTENT_BUILD
    return None


def resolve_auto(
    last_user_text: str, build_state: dict[str, Any]
) -> tuple[str, frozenset[str] | None]:
    """AUTO's per-message behaviour: (system prompt, allowed tool names).

    None = all tools. Routed-plan gets the restricted discovery set so it
    literally cannot build until the user confirms (the router then flips).
    A build interview in flight (build_args stashed by the gate) is STICKY —
    slot answers and uploads must keep the build behaviour. A message carrying
    attached-file content that is NOT a build request routes to ANSWER (reply
    from the content, zero tools) — a question with a document is a question.
    O(len(text))."""
    if build_state.get("build_args"):
        return prompt_for_mode("build"), _build_tool_names()
    words, has_attachments = split_attachments(last_user_text)
    intent = route_intent(words)
    if intent == INTENT_BUILD:
        return prompt_for_mode("build"), _build_tool_names()
    if intent == INTENT_PLAN:
        return _PLAN_PROMPT_AUTO, PLAN_TOOL_NAMES
    if has_attachments:
        # LIVE BUG: chart requests with an attached file were swallowed by the
        # answer-only route (zero tools -> the model "analyzed" the CSV in prose
        # and no dashboard could ever fire). Chart intent keeps the full toolset;
        # the graph's data-dashboard kick backs it deterministically.
        if wants_data_dashboard(words):
            return _AUTO_PROMPT, None
        return _ANSWER_PROMPT, frozenset()  # answer-only: no tools at all
    return _AUTO_PROMPT, None
