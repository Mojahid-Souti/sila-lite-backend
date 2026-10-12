"""Per-agent-type required-slot schema + the deterministic build gate (Part B).

A 4B/9B model won't reliably self-gate from prose, so the build tool enforces the
requirements interview in CODE — exactly like the request_documents-before-ask_user
ordering gate. The build tool walks the slot list for the classified agent type and
refuses to build until every must_ask slot is filled, the destination resource for
any connected provider is captured, and the user has confirmed the reflected spec.

`build_state` (a per-connection dict, like pending_credentials) carries:
  {agent_type, filled: {slot: answer}, asked: slot|None, confirmed: bool}

Complexity: O(slots) per gate evaluation; slots per type < 8.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

# Agent types — the required-slot list depends on which one applies.
# Briefs that pull from the open web rather than the user's files. A publisher
# sourced this way must NOT be asked to upload documents — the build used to ask
# every scheduled publisher, so "summarise the latest news from the web" opened
# an upload panel the user had nothing to put in.
_WEB_SOURCED = re.compile(
    r"\b(from the (web|internet)|latest news|news (from|on|about)|headlines|"
    r"web search|search the (web|internet)|online sources|rss|market data|"
    r"press releases|filings)\b",
    re.IGNORECASE,
)


def is_web_sourced(purpose: str, content: str = "") -> bool:
    """Does this agent draw on the open web rather than the user's files?

    Used for display (which icon the attachment card shows) as well as the
    upload gate, so both read from one definition of "web-sourced".
    """
    return bool(_WEB_SOURCED.search(f"{purpose} {content}"))


def needs_documents(agent_type: str, purpose: str, content: str = "") -> bool:
    """Should the build ask the user to upload documents?

    True only when the agent actually reads the user's OWN files. A knowledge
    agent always does — that is what it is. A scheduled publisher does only when
    its material is the user's documents; one briefing from the web needs none,
    and asking reads as the assistant not having listened.

    O(len) over the brief.
    """
    if agent_type == KNOWLEDGE:
        return True
    if agent_type != SCHEDULED_PUBLISHER:
        return False
    return not is_web_sourced(purpose, content)


KNOWLEDGE = "knowledge"
SCHEDULED_PUBLISHER = "scheduled_publisher"
MONITOR = "monitor"
WORKFLOW = "workflow"


@dataclass(frozen=True)
class Slot:
    name: str
    question: str
    options: tuple[str, ...] = ()
    must_ask: bool = True  # False = defaultable (assume the default, don't ask)
    default: str | None = None
    # When set, this slot is the DESTINATION for a connector and is only required
    # if that provider is among the agent's integrations (e.g. the Slack channel).
    destination_for: str | None = None


# Each list is ordered by what most changes the build / carries the most risk.
REQUIRED_SLOTS: dict[str, tuple[Slot, ...]] = {
    KNOWLEDGE: (
        Slot(
            "out_of_scope",
            "When the answer isn't in the documents, what should it do?",
            ("Say it doesn't know and point to a human", "Give a best-effort answer"),
        ),
        Slot(
            "escalation",
            "Which topics must it escalate to a human instead of answering?",
            ("Legal / compliance", "Disciplinary / HR-sensitive", "Nothing — answer everything"),
        ),
        Slot("name", "What should it be called?", must_ask=False, default="Knowledge Assistant"),
    ),
    SCHEDULED_PUBLISHER: (
        Slot(
            "content",
            "What should each post actually contain? (a daily summary of one fixed doc just"
            " repeats the same post)",
            (
                "The latest news from the web on a topic",
                "A different section each day",
                "A summary of only newly-added documents",
                "An answer to a set daily question",
            ),
        ),
        Slot(
            "schedule",
            "When should it run?",
            ("Every weekday at 9:00 AM", "Every day at 9:00 AM", "Weekly on Monday at 9:00 AM"),
        ),
        Slot(
            "destination",
            "Which Slack channel should it post to?",
            (),
            destination_for="slack",
        ),
        Slot(
            "email_to",
            "Which email address should the summary be sent to?",
            (),
            destination_for="email",
        ),
        # Google connectors take NO resource picker (least-privilege scopes), so the
        # target is captured here in the interview instead — see manifest slot_arg.
        Slot(
            "gmail_to",
            "Which email address should it send to (via your Gmail)?",
            (),
            destination_for="gmail",
        ),
        Slot(
            "sheet_id",
            "Which Google Sheet should it write to? Paste the spreadsheet link or its ID.",
            (),
            destination_for="google-sheets",
        ),
        Slot(
            "chat_space",
            "Which Google Chat space should it post to? Paste the space link or its ID.",
            (),
            destination_for="google-chat",
        ),
        Slot(
            "outlook_to",
            "Which email address should it send to (via your Outlook)?",
            (),
            destination_for="outlook-mail",
        ),
        Slot(
            "teams_channel",
            "Which Teams channel should it post to? In Teams: channel → ⋯ → "
            "'Get link to channel', then paste it here.",
            (),
            destination_for="ms-teams",
        ),
        Slot(
            "excel_link",
            "Which Excel file should it write to? In OneDrive/Excel: Share → "
            "'Copy link', then paste it here.",
            (),
            destination_for="ms-excel",
        ),
        Slot(
            "clickup_list",
            "Which ClickUp list should tasks go to? Open the list in ClickUp and "
            "paste its URL (or the list ID).",
            (),
            destination_for="clickup",
        ),
        Slot(
            "empty_behavior",
            "If there's nothing new to post that day, what should it do?",
            must_ask=False,
            default="skip the day",
        ),
        Slot("name", "What should it be called?", must_ask=False, default="Daily Post Agent"),
    ),
    MONITOR: (
        Slot("trigger", "What event should it react to?", ()),
        Slot("condition", "Under what condition should it fire?", ()),
        Slot("action", "What should it do when it fires?", ()),
        Slot("destination", "Where should the result go?", (), destination_for="slack"),
    ),
    WORKFLOW: (
        Slot("steps", "What are the steps it should perform, in order?", ()),
        Slot(
            "success",
            "How do you know it succeeded?",
            (),
            must_ask=False,
            default="completes all steps",
        ),
    ),
}

# Words in the request/purpose that signal each type. Checked in priority order.
_PUBLISHER_HINTS = (
    "post",
    "publish",
    "daily",
    "weekly",
    "schedule",
    "summary",
    "digest",
    "every ",
    "each morning",
    "each day",
    "send a summary",
)
_MONITOR_HINTS = ("when ", "trigger", "monitor", "watch", "react", "incoming", "whenever")
_WORKFLOW_HINTS = ("workflow", "multi-step", "multistep", "steps", "pipeline")
# A wired delivery destination is a strong publisher signal — a pure Q&A agent doesn't
# push to a channel or email a summary on a schedule. Used when the purpose is too thin.
_POST_DESTINATIONS = {
    "slack",
    "teams",
    "discord",
    "telegram",
    "email",
    "gmail",
    "google-sheets",
    "google-chat",
    "google-tasks",
    "outlook-mail",
    "ms-teams",
    "ms-todo",
    "ms-excel",
}


def classify_agent_type(purpose: str, integrations: list[str] | None = None) -> str:
    """Heuristic type classification from the request/purpose AND the wired
    integrations. Defaults to KNOWLEDGE (a document Q&A agent) — the safest, most
    common shape. n = len(purpose); O(n)."""
    t = (purpose or "").lower()
    integ = {i.lower() for i in (integrations or [])}
    # Event-driven phrasing is the most distinctive — check it first so "when X
    # happens, post to Slack" stays a monitor (it has its own Slack destination slot),
    # not a scheduled publisher.
    if any(h in t for h in _MONITOR_HINTS):
        return MONITOR
    if any(h in t for h in _WORKFLOW_HINTS):
        return WORKFLOW
    if (integ & _POST_DESTINATIONS) or any(h in t for h in _PUBLISHER_HINTS):
        return SCHEDULED_PUBLISHER
    return KNOWLEDGE


# Arabic block + supplement — presence in the user's OWN words marks an Arabic ask.
_ARABIC_RE = re.compile("[\u0600-\u06ff\u0750-\u077f]")


def detect_goal_language(text: str) -> str:
    """'ar' when the user's own words contain Arabic script, else 'en'. Deterministic
    (never model-guessed) — drives the output language the built agent and its
    preview are instructed to write in. O(len)."""
    return "ar" if _ARABIC_RE.search(text or "") else "en"


# Users paste whole URLs, not bare ids — accept both.
_SHEET_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
# Chat web URLs: chat.google.com/room/<id> or …#chat/space/<id>.
_CHAT_SPACE_RE = re.compile(r"(?:room|space)/([A-Za-z0-9_-]+)")
# Teams "Get link to channel": …/l/channel/<url-encoded channel id>/<name>?groupId=<team>…
_TEAMS_LINK_RE = re.compile(r"/l/channel/([^/]+)/[^?]*\?.*groupId=([0-9a-fA-F-]{36})")

# ClickUp list URL → the numeric list id ("…app.clickup.com/123/v/li/901234…").
_CLICKUP_LIST_RE = re.compile(r"/li/(\d+)")


# "the name should be: My first Agent" → "My first Agent" — typed corrections carry
# the value inside a sentence; storing the raw sentence made IT the literal value.
_NAME_LEAD_RE = re.compile(r"^\s*(?:the\s+)?name\s*(?:should\s+be|is|=)?\s*[:\-]?\s*", re.I)
_CALL_IT_RE = re.compile(r"(?:call\s+it|rename\s+(?:it\s+)?(?:to\s+)?)\s+(.+)$", re.I)
_EMAIL_ADDR_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SLACK_CHANNEL_RE = re.compile(r"#([A-Za-z0-9_-]+)")


def extract_correction(slot_name: str, text: str) -> str:
    """Pull the VALUE out of a typed correction. Deterministic per slot: name strips
    the lead-in phrase; email slots take the address token; the Slack destination
    takes the #channel; sheet/chat slots run the URL normalizer; everything else
    passes through trimmed (schedule phrases are parsed downstream). O(len)."""
    t = text.strip()
    if slot_name == "name":
        m = _CALL_IT_RE.search(t)
        cleaned = m.group(1) if m else _NAME_LEAD_RE.sub("", t)
        return (cleaned.strip().strip("\"'“”").strip() or t)[:60]
    if slot_name in ("gmail_to", "email_to", "outlook_to"):
        m = _EMAIL_ADDR_RE.search(t)
        return m.group(0) if m else t
    if slot_name == "destination":
        m = _SLACK_CHANNEL_RE.search(t)
        return f"#{m.group(1)}" if m else t
    return normalize_destination(slot_name, t)


def normalize_destination(slot_name: str, value: str) -> str:
    """Turn a captured destination answer into the value the payload builder needs
    (sheet_id: pasted URL → bare spreadsheet id; chat_space: URL or bare id →
    `spaces/<id>` resource name). Other slots pass through. O(len)."""
    text = value.strip()
    if slot_name == "sheet_id":
        match = _SHEET_URL_RE.search(text)
        return match.group(1) if match else text
    if slot_name == "chat_space":
        match = _CHAT_SPACE_RE.search(text)
        space_id = match.group(1) if match else text.strip("/").removeprefix("spaces/")
        return f"spaces/{space_id}"
    if slot_name == "teams_channel":
        # → "teamId/channelId" (what the Teams payload builder splits on).
        match = _TEAMS_LINK_RE.search(text)
        if match:
            return f"{match.group(2)}/{unquote(match.group(1))}"
        return text
    if slot_name == "clickup_list":
        match = _CLICKUP_LIST_RE.search(text)
        return match.group(1) if match else text
    return text


def next_missing_slot(
    agent_type: str,
    filled: dict[str, Any],
    integrations: list[str],
    skip_destination_for: set[str] | None = None,
) -> Slot | None:
    """The next must_ask slot still unfilled for this type, or None when complete.
    Defaultable slots are skipped (the default applies); a destination slot is only
    required when its provider is wired AND not in `skip_destination_for` (an OAuth
    provider whose destination is captured by the Connect resource picker, not a
    typed slot — so we don't ask the Slack channel twice)."""
    integ = {i.lower() for i in (integrations or [])}
    skip = skip_destination_for or set()
    for slot in REQUIRED_SLOTS.get(agent_type, ()):
        if slot.name in filled or not slot.must_ask:
            continue
        if slot.destination_for and (
            slot.destination_for not in integ or slot.destination_for in skip
        ):
            continue
        return slot
    return None


def missing_slots(
    agent_type: str,
    filled: dict[str, Any],
    integrations: list[str],
    skip_destination_for: set[str] | None = None,
) -> list[Slot]:
    """EVERY must_ask slot still unfilled — so the gate can ask them all in ONE sheet
    (a multi-step wizard) instead of one tool call per question. Same skip rules as
    next_missing_slot."""
    integ = {i.lower() for i in (integrations or [])}
    skip = skip_destination_for or set()
    out: list[Slot] = []
    for slot in REQUIRED_SLOTS.get(agent_type, ()):
        if slot.name in filled or not slot.must_ask:
            continue
        if slot.destination_for and (
            slot.destination_for not in integ or slot.destination_for in skip
        ):
            continue
        out.append(slot)
    return out


def reflect_spec(
    name: str, agent_type: str, filled: dict[str, Any], integrations: list[str]
) -> str:
    """One compact restated spec for the confirm step (step 6)."""
    label = {
        KNOWLEDGE: "Knowledge / Q&A",
        SCHEDULED_PUBLISHER: "Scheduled publisher",
        MONITOR: "Monitor / trigger",
        WORKFLOW: "Workflow automation",
    }.get(agent_type, agent_type)
    parts = [f"{name or 'Agent'} — {label}"]
    for slot in REQUIRED_SLOTS.get(agent_type, ()):
        val = filled.get(slot.name) or (slot.default if not slot.must_ask else None)
        if val:
            parts.append(f"{slot.name.replace('_', ' ')}: {val}")
    if integrations:
        parts.append(f"integrations: {', '.join(integrations)}")
    return "; ".join(parts) + ". Draft + Unvetted → security review."


# A seeded slot value is display text, never code — cap it so a hostile client
# can't stuff megabytes into connection memory / the reflected spec.
_SEED_VALUE_MAX = 300

# Answers that DELEGATE the choice ("Decide for me" on the sheet, or typed
# equivalents). Never parroted into the spec — the gate resolves them to a real
# value (or re-asks, for a destination that cannot be guessed).
_DECIDE_TOKENS = frozenset(
    {
        "decide for me",
        "decide",
        "idk",
        "i dont know",
        "i don't know",
        "you decide",
        "up to you",
        "whatever",
        "whatever works",
        "not sure",
        "choose for me",
        "anything",
        "you choose",
    }
)

# The tappable "delegate it" option appended to every sheet question (except
# destinations — a channel/recipient can't be guessed and must be answered).
DECIDE_OPTION = "Decide for me"


def _decide_default(slot: Slot, purpose: str) -> str:
    """The value a delegated slot resolves to: the safest listed option, the
    slot's default, or a purpose-derived instruction the build can act on."""
    if slot.name == "steps":
        return f"derive the steps from the goal: {purpose[:120]}" if purpose else "derive from goal"
    if slot.options:
        return slot.options[0]
    if slot.default:
        return slot.default
    return f"SILA's best choice for the goal: {purpose[:80]}" if purpose else "SILA's best choice"


def resolve_undecided(agent_type: str, filled: dict[str, Any], purpose: str) -> None:
    """Replace delegated answers ('Decide for me', 'idk', …) with a real derived
    value, in place. A delegated DESTINATION is deleted instead so the gate
    re-asks it — a recipient/channel can never be guessed. O(slots)."""
    for slot in REQUIRED_SLOTS.get(agent_type, ()):
        raw = filled.get(slot.name)
        if raw is None:
            continue
        if str(raw).strip().lower().rstrip(".!") in _DECIDE_TOKENS:
            if slot.destination_for:
                del filled[slot.name]  # must be answered — re-ask
            else:
                filled[slot.name] = _decide_default(slot, purpose)


def merge_slot_overrides(state: dict[str, Any], agent_type: str, raw: Any) -> bool:
    """Merge LLM-supplied corrections ({slot: new value}) into the interview —
    the 'Let me change something' path. Whitelisted per type, values capped;
    returns True if anything changed (the caller then re-confirms with a fresh
    preview). The gate stays authoritative — this only EDITS answers. O(slots)."""
    if not isinstance(raw, dict):
        return False
    allowed = {s.name for s in REQUIRED_SLOTS.get(agent_type, ())}
    filled = state.setdefault("filled", {})
    changed = False
    for key, value in raw.items():
        if key in allowed and isinstance(value, str | int | float):
            text = str(value).strip()[:_SEED_VALUE_MAX]
            if text and filled.get(key) != text:
                filled[key] = text
                changed = True
    return changed


def sanitize_seed(raw: Any) -> dict[str, Any]:
    """Validate a client-supplied build seed (the Plan→Build handoff) into a safe
    build_state fragment: {agent_type, filled, build_args?}.

    Never trusted as-is: the agent type must be a known type, slot keys must belong
    to that type's slot list, and values are length-capped plain strings. Anything
    else is dropped; garbage in → {} out. `build_args` makes the handoff FULLY
    deterministic: the pause node re-runs the build tool with these stashed args,
    so a weak model never gets to reinvent the name/purpose after Accept. Seeding
    only FILLS answers — the requirements gate still validates every slot and
    still requires the user's confirm, so a seed can never bypass the guard.
    O(slots)."""
    if not isinstance(raw, dict):
        return {}
    agent_type = str(raw.get("agent_type") or "").strip()
    if agent_type not in REQUIRED_SLOTS:
        return {}
    allowed = {s.name for s in REQUIRED_SLOTS[agent_type]}
    filled: dict[str, str] = {}
    filled_raw = raw.get("filled")
    if isinstance(filled_raw, dict):
        for key, value in filled_raw.items():
            if key in allowed and isinstance(value, str | int | float):
                text = str(value).strip()[:_SEED_VALUE_MAX]
                if text:
                    filled[key] = text
    out: dict[str, Any] = {"agent_type": agent_type, "filled": filled}
    bargs_raw = raw.get("build_args")
    if isinstance(bargs_raw, dict):
        name = str(bargs_raw.get("name") or "").strip()[:60]
        purpose = str(bargs_raw.get("purpose") or "").strip()[:500]
        integ_raw = bargs_raw.get("integrations")
        integrations = (
            [str(i).strip().lower()[:40] for i in integ_raw[:5] if str(i).strip()]
            if isinstance(integ_raw, list)
            else []
        )
        if name and purpose:
            out["build_args"] = {"name": name, "purpose": purpose, "integrations": integrations}
    return out


@dataclass
class BuildState:
    """Mutable per-connection build interview state (shared like pending_credentials).
    Used as a plain dict at the call sites; this documents the shape."""

    agent_type: str | None = None
    filled: dict[str, Any] = field(default_factory=dict)
    asked: str | None = None
    confirmed: bool = False
