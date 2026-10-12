"""SILA tool registry — the curated, Builder-scope set of actions the
concierge may take on the user's behalf.

Security contract (phase security review):
  - Every tool wraps an EXISTING service call with the user's own context, so
    RLS/OPA still enforce; the worst possible outcome is a denied result.
  - No delete, no publish/approve, no member/role operations — ever.
  - Per-tool `min_role` is checked at execution time against the caller's
    membership role; failures return a polite refusal to the model (and the
    user), never an escalation.

Complexity: registry lookups O(1) (dict); tools delegate to indexed queries.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.auth.base import UserResult
from sila.adapters.search.meilisearch import MeilisearchAdapter
from sila.core.config import settings
from sila.core.constants import ROLE_RANK, Role
from sila.services.bazaar_service import BazaarService
from sila.services.sila.build_slots import DECIDE_OPTION as _DECIDE_OPTION

log = structlog.get_logger("sila.tools")

# build_state key: the session carries confidential (OKR) data — set by the
# session's ResidencyGateway, read by tools that would keep or send data.
CONFIDENTIAL_KEY = "confidential"

# Tools that send data, or a query built from it, off this server — to the
# web, a mailbox, a connected Microsoft 365 / Power BI / Grafana, or an
# external MCP server. A confidential (OKR) session never runs them: its deck
# stays in Sila.
LEAVES_THE_BOX = frozenset(
    {
        "fetch_url",
        "web_search",
        "send_email",
        "confirm_send_email",
        "create_powerbi_report",
        "create_grafana_dashboard",
        "create_slide_deck",
        "create_docx",
        "create_report_docx",
        "create_insight_deck",
        "pdd_export",
    }
)
_STAYS_IN_SILA = (
    "NOT DONE — this conversation carries a confidential OKR sheet, so nothing from it "
    "leaves Sila: no web, email, cloud drive or external service. The deck and the "
    "dashboard are available here as downloads. Do not offer another destination."
)

# A tool that runs longer than this is hung, not working — the longest honest
# path (create_agent_from_docs: KB build + vetting) stays well under it.
_TOOL_TIMEOUT_S = 300.0


@dataclass
class ToolContext:
    db: AsyncSession
    user: UserResult
    workspace_id: uuid.UUID
    role: Role
    # When True (the SILA graph path), build tools register the Draft agent but
    # DON'T submit it to the gatekeeper themselves — they hand the agent id back
    # via ToolResult.meta so the graph's vetting node runs the review *visibly*.
    defer_vetting: bool = False
    # provider → (n8n credential type, cred id) collected via the Incognito panel; the
    # build tool wires these into the workflow's integration nodes. Never a secret.
    pending_credentials: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Part B — the deterministic requirements-interview state for create_agent_workflow:
    # {agent_type, filled: {slot: answer}, asked: slot|None, confirmed: bool}. Shared with
    # the WS handler, which records ask_user answers into it on resume.
    build_state: dict[str, Any] = field(default_factory=dict)
    # The interactive (supervisor) path can pause for the requirements interview; an
    # autonomous fan-out worker CANNOT (§C pause-free), so it skips the gate.
    interactive: bool = True
    # Live progress channel: a tool calls ctx.report(...) at REAL stage boundaries
    # ("Designing the layout…") and the orchestrator streams it under the running
    # step in the trace. Set per-call by the executor; None outside a turn.
    # Called as progress(note) for a plain stage note and progress(note, payload)
    # for a counted read — so a one-argument channel (every existing caller and
    # test stub) keeps working untouched.
    progress: Callable[..., None] | None = None

    def report(self, note: str) -> None:
        """Emit a progress note if a channel is wired (never raises)."""
        if self.progress is not None:
            # Progress is cosmetic — a broken channel must never fail the tool.
            with contextlib.suppress(Exception):
                self.progress(note)

    def report_progress(
        self, note: str, *, done: int, total: int, item: dict[str, Any] | None = None
    ) -> None:
        """A counted step — "Reading 3 of 23" — with the item just handled.

        Rides the same channel as `report`, carrying a structured payload the
        trace renders as a percentage and a scrolling list, so reading an inbox
        looks like the web-search step does instead of a bare label.
        """
        if self.progress is not None:
            with contextlib.suppress(Exception):
                self.progress(note, {"done": done, "total": total, "item": item})


@dataclass(frozen=True)
class ToolResult:
    """What a tool produced: text for the model, a short display summary shown
    under the step in the trace, an optional UI command, and optional non-UI
    metadata the orchestrator consumes (e.g. a freshly-built agent id to vet)."""

    content: str
    summary: str | None = None
    ui: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class SilaTool:
    name: str
    description: str
    parameters: dict[str, Any]
    min_role: Role
    run: Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]
    extra: dict[str, Any] = field(default_factory=dict)


def _bazaar_service(db: AsyncSession) -> BazaarService:
    search = MeilisearchAdapter(
        url=settings.meilisearch_url, master_key=settings.meilisearch_master_key
    )
    return BazaarService(db, search)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_NAV_PATHS: dict[str, str] = {
    "bazaar": "/bazaar",
    "hired": "/bazaar/hired",
    # SILA's audience lives in the Simple shell — "your agents" is /my-agents
    # (the clean per-workspace card grid), never the builder dashboard page.
    "agents": "/my-agents",
    "workflows": "/workspaces/{wid}/workflows",
    "knowledge_base": "/workspaces/{wid}/knowledge-base",
    "dashboard": "/dashboard",
}


async def _navigate(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    page = str(args.get("page", "")).lower()
    template = _NAV_PATHS.get(page)
    if not template:
        return ToolResult(content=f"Unknown page '{page}'. Valid: {', '.join(_NAV_PATHS)}.")
    path = template.replace("{wid}", str(ctx.workspace_id))
    return ToolResult(
        content=f"Opened {page} for the user.",
        summary=f"Opened {page.replace('_', ' ')}",
        ui={"command": "navigate", "path": path},
    )


async def _fetch_url(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Fetch a web page the user named and hand back its readable text so SILA can
    summarize/extract from it. SSRF-guarded, size-bounded, read-only (no JS)."""
    from sila.services.sila.web_fetch import fetch_readable

    url = str(args.get("url", "")).strip()
    if not url:
        return ToolResult(content="No URL was provided to fetch.")
    ctx.report("Downloading the page…")
    try:
        title, text = await fetch_readable(url)
    except ValueError as exc:
        return ToolResult(content=f"Couldn't read that page: {exc}", summary="Fetch failed")
    if not text.strip():
        return ToolResult(content="The page had no readable text.", summary="Nothing to read")
    header = f"Title: {title}\n\n" if title else ""
    return ToolResult(
        content=f"{header}Content from {url}:\n\n{text}",
        summary=f"Read {title or url}"[:80],
    )


async def _web_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Search the web (self-hosted SearXNG) and hand back the top results so SILA can
    answer a current/factual question, or pick a page to open with fetch_url."""
    from sila.services.sila.web_search import SearchUnavailableError, search_web

    query = str(args.get("query", "")).strip()
    if not query:
        return ToolResult(content="No search query was provided.")
    ctx.report(f"Querying the search engines for \u201c{query[:60]}\u201d…")
    try:
        hits = await search_web(query)
    except SearchUnavailableError as exc:
        # Say what actually happened. "No results" reads as a fact about the
        # world and sends the model rephrasing a query that was never at fault —
        # which fires more requests at engines that are blocking us precisely
        # because of request volume.
        return ToolResult(
            content=(
                f"{exc} Do NOT retry with different wording — tell the user web "
                "search is temporarily unavailable, and use fetch_url if you "
                "already know a specific page to read."
            ),
            summary="Search unavailable",
        )
    except ValueError as exc:
        return ToolResult(content=f"Couldn't search: {exc}", summary="Search failed")
    if not hits:
        return ToolResult(
            content=(
                f"No matches for '{query}'. Try ONE more phrasing at most, then "
                "tell the user nothing was found rather than searching again."
            ),
            summary="No results",
        )
    # Hand the model clean, clickable material: a markdown link per result + snippet,
    # so it can relay them directly (the UI renders [title](url) as a link).
    lines = [f"Search results for '{query}':", ""]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. [{h.title}]({h.url}) — {h.snippet}")
    # The sources ride the RESULT, not the prose. Relying on the model to repeat
    # the markdown meant the citation list appeared only when it felt like it;
    # the UI now renders these deterministically under the step.
    sources = [
        {"title": h.title, "url": h.url, "domain": urlparse(h.url).netloc.removeprefix("www.")}
        for h in hits
    ]
    return ToolResult(
        content="\n".join(lines),
        summary=f"Searched the web · {len(hits)} sources",
        meta={"sources": sources},
    )


# ---------------------------------------------------------------------------
# Connector READS (Data-Analyst / PDD templates) — fetch through the user's
# OWN binding, then stash the result as a virtual "uploaded file" so every
# downstream data tool (dashboard / Power BI / Grafana / slides) consumes it
# unchanged. Session memory only — nothing lands in the KB automatically.
# ---------------------------------------------------------------------------


def _stash_virtual_file(ctx: ToolContext, name: str, content: str) -> None:
    """Same shape as runner.stash_attachments produces; same-name replaces."""
    files = ctx.build_state.setdefault("uploaded_files", [])
    files[:] = [f for f in files if f.get("name") != name]
    # Flagged so the data tools can prefer what the USER attached over what a
    # reader stashed: "most recent wins" sent a Grafana dashboard to the mail
    # export instead of the performance file the user had just asked about.
    files.append({"name": name, "content": content, "virtual": True})


def _pick_data_file(
    ctx: ToolContext, data_files: list[dict[str, Any]], wanted: str = ""
) -> dict[str, Any]:
    """Which of several data files a tool should act on.

    1. A file the model NAMED wins — unless it named a connector stash while
       the user has a real spreadsheet attached. The model asked to chart
       'outlook-emails.csv' after reading a mailbox and got a one-row dashboard
       of sender addresses, with the attached performance file untouched. An
       inbox export is never the dataset when the user supplied one.
    2. The file the in-chat dashboard was just built from — a Grafana push or a
       Power BI report after a dashboard means THAT data, not whatever landed
       last.
    3. A file the user attached, over one a connector read stashed.
    4. The most recent.

    O(n) over a handful of files.
    """
    attached = [f for f in data_files if not f.get("virtual")]
    if wanted:
        named = [f for f in data_files if wanted in str(f.get("name", "")).lower()]
        if named and not (named[-1].get("virtual") and attached):
            return named[-1]
    source = str(ctx.build_state.get("dashboard_source") or "")
    if source:
        for f in reversed(data_files):
            if str(f.get("name")) == source:
                return f
    return (attached or data_files)[-1]


def _connect_needed(provider: str, label: str) -> ToolResult:
    """Pause with the Connect panel; the model retries the read after consent."""
    return ToolResult(
        content=(
            f"{label} isn't connected yet. Opening the secure connect panel — once "
            "the user authorizes it, call the read again."
        ),
        summary=f"Connect {label}",
        ui={
            "command": "connect_provider",
            "request_id": str(uuid.uuid4()),
            "provider": provider,
            "label": label,
        },
    )


_NEXT_STEP_NOTE = (
    "The data is now attached to this session. Describe it in 1–2 sentences, then ask "
    "which output the user wants — a dashboard right here, a slide deck, Power BI, or "
    "Grafana — unless they already said."
)

# The message text the model is handed in the tool result. Telling it the text
# was "in a file" without giving it the text is what made it re-read the inbox
# after every step: it had nothing to quote, so it fetched again. 12 KB covers
# a week of real mail; past that the stash still holds the rest.
_MESSAGES_EXCERPT_CHARS = 12_000


def _messages_memo(ctx: ToolContext, key: str) -> dict[str, Any] | None:
    """A read already done this session with the same arguments. Returned from
    memory instead of fetched again — the model called the same read four times
    in one turn, each one a fresh Graph call and a fresh "Reading your inbox"
    step on screen. `refresh: true` on the call bypasses it."""
    memo = ctx.build_state.get("message_reads")
    if isinstance(memo, dict):
        hit = memo.get(key)
        return hit if isinstance(hit, dict) else None
    return None


def _remember_messages(ctx: ToolContext, key: str, result: dict[str, Any]) -> None:
    ctx.build_state.setdefault("message_reads", {})[key] = result


# A mailbox or a chat channel is CONTEXT, not a dataset. The generic note above
# told the model its export was "data now attached", and the model duly charted
# an inbox: a one-row dashboard of sender addresses, while the spreadsheet the
# user had attached went untouched. Messages are read and quoted, never plotted.
_MESSAGES_NOTE = (
    "These messages are CONTEXT to read and quote — they are NOT a dataset. Do NOT "
    "call analyze_data or create_data_dashboard on them: they carry no numeric "
    "columns, so both refuse. Read the text, pull out the specific items the user "
    "asked for (names, figures, dates — quote them), and chart ONLY a real "
    "spreadsheet. If the user attached one, that file is the dataset."
)


async def _read_google_sheet(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from sila.services.connectors import reads
    from sila.services.connectors.connector_service import ConnectorError

    ref = str(args.get("sheet") or args.get("url") or "").strip()
    if not ref:
        return ToolResult(content="No sheet link or id was provided.")
    ctx.report("Reading the sheet…")
    try:
        name, csv_text = await reads.read_google_sheet(
            ctx.db,
            user_id=ctx.user.id,
            workspace_id=ctx.workspace_id,
            ref=ref,
            cell_range=str(args.get("range") or "").strip() or None,
        )
    except ConnectorError as exc:
        if "not connected" in str(exc):
            return _connect_needed("google-sheets", "Google Sheets")
        return ToolResult(content=f"Couldn't read the sheet: {exc}", summary="Sheet read failed")
    _stash_virtual_file(ctx, name, csv_text)
    rows = max(csv_text.count("\n") - 1, 0)
    return ToolResult(
        content=f"Read {rows} data rows from the sheet into '{name}'. {_NEXT_STEP_NOTE}",
        summary=f"Read sheet · {rows} rows",
    )


async def _read_outlook_emails(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from sila.services.connectors import reads
    from sila.services.connectors.connector_service import ConnectorError, NoMessagesError

    folder = str(args.get("folder") or "inbox")
    search = str(args.get("search") or "").strip() or None
    since_days = int(args.get("since_days") or 7)
    limit = int(args.get("limit") or 25)
    memo_key = f"outlook|{folder}|{search or ''}|{since_days}|{limit}"
    cached = None if args.get("refresh") else _messages_memo(ctx, memo_key)
    if cached is not None:
        plural = "email" if cached["count"] == 1 else "emails"
        return ToolResult(
            content=(
                f"Already read this session — {cached['count']} {plural}. Do NOT read again; "
                f"here is the text:\n\n{cached['excerpt']}\n\n{_MESSAGES_NOTE}"
            ),
            summary=f"Using the {cached['count']} {plural} already read",
        )
    ctx.report("Reading your Outlook mail…")
    # The message count comes from the reader's own per-item callback. Counting
    # newlines in the CSV counted the ones inside quoted body previews too, so a
    # single email was reported as "Read 5 emails" — a number the model then
    # repeated to the user as fact.
    seen = {"total": 0}

    def _on_item(done: int, total: int, item: dict[str, Any]) -> None:
        seen["total"] = total
        ctx.report_progress(f"Reading {done} of {total}…", done=done, total=total, item=item)

    try:
        csv_name, csv_text, txt_name, txt_text = await reads.read_outlook_emails(
            ctx.db,
            user_id=ctx.user.id,
            workspace_id=ctx.workspace_id,
            folder=folder,
            search=search,
            since_days=since_days,
            limit=limit,
            on_item=_on_item,
        )
    except NoMessagesError:
        # The read worked; the window was empty. Saying "failed" misled users and
        # the model alike — say what happened and how to look further back.
        window = f"in the last {since_days} days" if not search else f'matching "{search}"'
        return ToolResult(
            content=(
                f"The {folder} folder has no emails {window}. If the user wants older "
                "mail, read again with a larger since_days."
            ),
            summary=f"No emails {window}",
        )
    except ConnectorError as exc:
        if "not connected" in str(exc):
            return _connect_needed("outlook-mail", "Outlook")
        return ToolResult(content=f"Couldn't read the mail: {exc}", summary="Mail read failed")
    _stash_virtual_file(ctx, csv_name, csv_text)
    _stash_virtual_file(ctx, txt_name, txt_text)
    count = seen["total"]
    plural = "email" if count == 1 else "emails"
    excerpt = txt_text[:_MESSAGES_EXCERPT_CHARS]
    _remember_messages(ctx, memo_key, {"count": count, "excerpt": excerpt})
    return ToolResult(
        content=(
            f"Read {count} {plural}. You now HAVE the text below — do not read the "
            f"inbox again this turn. Quote what the user asked for from it.\n\n"
            f"{excerpt}\n\n{_MESSAGES_NOTE}"
        ),
        summary=f"Read {count} {plural}",
    )


# Attachments we can decode as text right now (data + text). Office/PDF binary
# parsing routes through the KB pipeline — a follow-up, captured-and-flagged here.
_TEXT_ATTACHMENT_EXTS = {"csv", "tsv", "txt", "md", "json", "xml", "yaml", "yml", "log", "html"}


async def _read_email_attachments(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from sila.services.connectors import reads
    from sila.services.connectors.connector_service import ConnectorError

    subject = str(args.get("subject") or args.get("search") or "").strip() or None
    ctx.report("Reading the email's attachments…")
    try:
        matched_subject, attachments = await reads.read_email_attachments(
            ctx.db,
            user_id=ctx.user.id,
            workspace_id=ctx.workspace_id,
            subject=subject,
            folder=str(args.get("folder") or "inbox"),
        )
    except ConnectorError as exc:
        if "not connected" in str(exc):
            return _connect_needed("outlook-mail", "Outlook")
        return ToolResult(
            content=f"Couldn't read attachments: {exc}", summary="Attachment read failed"
        )
    if not attachments:
        return ToolResult(content="That email has no file attachments.", summary="No attachments")

    stashed: list[str] = []
    skipped: list[str] = []
    for name, _ctype, raw in attachments:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in _TEXT_ATTACHMENT_EXTS:
            _stash_virtual_file(ctx, name, raw.decode("utf-8", "replace"))
            stashed.append(name)
        else:
            skipped.append(f"{name} ({max(len(raw) // 1024, 1)} KB)")
    parts: list[str] = []
    if stashed:
        parts.append(
            f"Attached to this session from '{matched_subject[:50]}': {', '.join(stashed)}. "
            + _NEXT_STEP_NOTE
        )
    if skipped:
        parts.append(
            "Captured but not yet parseable (binary — Office/PDF extraction is the next "
            f"step): {', '.join(skipped)}."
        )
    return ToolResult(
        content=" ".join(parts) or "No readable attachments found.",
        summary=f"Read {len(stashed)} attachment(s)",
    )


async def _read_meeting_transcript(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Pull a Teams online-meeting transcript into the session so SILA can
    summarize it, pull action items, draft an email, etc."""
    from sila.services.connectors import reads
    from sila.services.connectors.connector_service import ConnectorError

    meeting = str(args.get("meeting") or args.get("link") or args.get("url") or "").strip() or None
    ctx.report("Fetching the meeting transcript…")
    try:
        subject, text = await reads.read_meeting_transcript(
            ctx.db, user_id=ctx.user.id, workspace_id=ctx.workspace_id, meeting=meeting
        )
    except ConnectorError as exc:
        if "not connected" in str(exc):
            return _connect_needed("ms-teams", "Microsoft Teams")
        return ToolResult(content=f"Couldn't read the transcript: {exc}", summary="No transcript")
    fname = f"meeting-transcript-{re.sub(r'[^a-z0-9]+', '-', subject.lower())[:40] or 'latest'}.txt"
    _stash_virtual_file(ctx, fname, f"# {subject}\n\n{text}")
    # A short head so the model can react immediately; the full text is the file.
    head = text[:1200]
    return ToolResult(
        content=(
            f"Transcript of '{subject}' attached to this session as {fname} "
            f"({len(text)} chars). Beginning:\n\n{head}\n\n"
            "Use it to answer the user — summarize, pull decisions/action items, or "
            "draft a follow-up. Do NOT invent anything that isn't in the transcript."
        ),
        summary=f"Read transcript — {subject}"[:80],
    )


# Deliberately permissive: this rejects what CANNOT work (no "@", whitespace, a
# domain with no dot) and waves through everything else. Full RFC 5322 is not the
# job here — the provider is the authority on whether a mailbox exists.
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]{2,}$")


async def _send_email(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Prepare an outbound email and PAUSE for the user to confirm in an
    Outlook-branded panel — nothing is sent here. The draft is stashed and only
    confirm_send_email (dispatched deterministically when the user hits Send)
    actually delivers it. Called only when the user explicitly asks to send/reply."""
    to = str(args.get("to") or "").strip()
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or args.get("text") or args.get("content") or "").strip()
    greeting = str(args.get("greeting") or "").strip() or None
    if not to or not body:
        return ToolResult(content="I need both a recipient (`to`) and a message body to send.")
    # An address, not a name. Graph resolves nothing: it answers a display name
    # with ErrorInvalidRecipients, and it does so only at SEND time — so the
    # draft appeared, the user pressed Send, and the message died with no
    # explanation. Checking here means the model can ask for the address before
    # the confirm panel ever opens.
    if not _EMAIL_RE.match(to):
        return ToolResult(
            content=(
                f"'{to}' is not an email address, and the mail provider cannot resolve "
                "a name into one. Ask the user for the full address (e.g. "
                "name@company.com) and call this again."
            ),
            summary="Need an email address",
        )
    # Optional primary call-to-action rendered as a button (e.g. the dashboard link).
    cta_raw = args.get("cta")
    cta = None
    if isinstance(cta_raw, dict) and str(cta_raw.get("url", "")).strip():
        cta = {
            "label": str(cta_raw.get("label") or "Open").strip(),
            "url": str(cta_raw["url"]).strip(),
        }
    request_id = uuid.uuid4().hex
    # Stash the draft for confirm_send_email; drop any stale confirmation flag so a
    # new draft always requires a fresh Send click.
    ctx.build_state["pending_email"] = {
        "to": to,
        "subject": subject or "(no subject)",
        "body": body,
        "greeting": greeting,
        "cta": cta,
    }
    ctx.build_state.pop("email_confirmed", None)
    return ToolResult(
        content=(
            f"Drafted an email to {to} — waiting for the user to review it in the "
            "confirm panel and hit Send. Do NOT say it was sent; the panel handles it."
        ),
        summary=f"Confirm email to {to}"[:80],
        ui={
            "command": "confirm_email",
            "request_id": request_id,
            "draft": {"to": to, "subject": subject or "(no subject)", "body": body},
        },
    )


async def _confirm_send_email(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Deliver the stashed draft AFTER the user confirmed in the panel. Hidden from
    the model's toolset: the pause node dispatches it and sets email_confirmed, so
    the model cannot bypass the confirm gate by calling it directly."""
    from sila.core.email_template import render_sila_email, sila_logo_inline
    from sila.services.connectors.connector_service import ConnectorError, ConnectorService

    draft = ctx.build_state.get("pending_email")
    if not isinstance(draft, dict) or not ctx.build_state.get("email_confirmed"):
        return ToolResult(content="No confirmed email draft to send.", summary="Nothing to send")
    to = str(draft.get("to") or "").strip()
    subject = str(draft.get("subject") or "(no subject)")
    body = str(draft.get("body") or "")
    greeting = draft.get("greeting") or None
    cta = draft.get("cta") if isinstance(draft.get("cta"), dict) else None
    html = render_sila_email(body=body, greeting=greeting, cta=cta)
    data: dict[str, Any] = {"to": to, "subject": subject, "text": body, "html": html}
    # Embed the Sila lockup inline (cid:) so it renders where external images don't.
    logo = sila_logo_inline()
    if logo is not None:
        content_id, blob, mime = logo
        data["inline_images"] = [
            {"content_id": content_id, "bytes": blob, "content_type": mime, "name": "sila-logo.png"}
        ]
    ctx.report(f"Sending the email to {to}…")
    try:
        await ConnectorService(ctx.db).execute_proxy(
            user_id=ctx.user.id,
            workspace_id=ctx.workspace_id,
            provider="outlook-mail",
            method="POST",
            endpoint="me/sendMail",
            data=data,
        )
    except ConnectorError as exc:
        if "not connected" in str(exc):
            return _connect_needed("outlook-mail", "Outlook")
        return ToolResult(content=f"Couldn't send the email: {exc}", summary="Send failed")
    finally:
        ctx.build_state.pop("pending_email", None)
        ctx.build_state.pop("email_confirmed", None)
    return ToolResult(
        content=f"Email sent to {to} (subject: {subject}).",
        summary=f"Emailed {to}"[:80],
    )


async def _read_teams_messages(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    from sila.services.connectors import reads
    from sila.services.connectors.connector_service import ConnectorError

    team_arg = str(args.get("team") or "").strip().casefold()
    channel_arg = str(args.get("channel") or "").strip().casefold()
    ctx.report("Looking up your Teams…")
    try:
        teams = await reads.list_teams_and_channels(
            ctx.db, user_id=ctx.user.id, workspace_id=ctx.workspace_id
        )
    except ConnectorError as exc:
        if "not connected" in str(exc):
            return _connect_needed("ms-teams", "Microsoft Teams")
        return ToolResult(content=f"Couldn't list your teams: {exc}", summary="Teams read failed")
    if not teams:
        return ToolResult(content="You're not a member of any team on this account.")

    # Resolve team + channel by NAME (case-insensitive substring); a single
    # option auto-picks so the user isn't asked pointless questions. O(teams).
    def _match(items: list[dict[str, Any]], want: str) -> dict[str, Any] | None:
        if not want:
            return items[0] if len(items) == 1 else None
        return next((i for i in items if want in str(i["name"]).casefold()), None)

    team = _match(teams, team_arg)
    if team is None:
        listing = "; ".join(
            f"{t['name']} (channels: {', '.join(c['name'] for c in t['channels'][:6])})"
            for t in teams
        )
        return ToolResult(
            content=f"Which team and channel? The user is in: {listing}. Ask, then call again.",
            summary="Which Teams channel?",
        )
    channel = _match(list(team["channels"]), channel_arg)
    if channel is None:
        names = ", ".join(c["name"] for c in team["channels"])
        return ToolResult(
            content=(
                f"'{team['name']}' has these channels: {names}. Ask which one, then call again."
            ),
            summary="Which channel?",
        )
    limit = int(args.get("limit") or 30)
    memo_key = f"teams|{team['id']}|{channel['id']}|{limit}"
    cached = None if args.get("refresh") else _messages_memo(ctx, memo_key)
    if cached is not None:
        return ToolResult(
            content=(
                f"Already read this session — {cached['count']} messages from "
                f"#{channel['name']}. Do NOT read again; here is the text:\n\n"
                f"{cached['excerpt']}\n\n{_MESSAGES_NOTE}"
            ),
            summary=f"Using the {cached['count']} messages already read",
        )
    ctx.report(f"Reading #{channel['name']}…")
    # Same as the mailbox: the reader counts, not a newline tally over text that
    # contains newlines of its own.
    seen = {"total": 0}

    def _on_item(done: int, total: int, item: dict[str, Any]) -> None:
        seen["total"] = total
        ctx.report_progress(f"Reading {done} of {total}…", done=done, total=total, item=item)

    try:
        txt_name, text = await reads.read_teams_messages(
            ctx.db,
            user_id=ctx.user.id,
            workspace_id=ctx.workspace_id,
            team_id=str(team["id"]),
            channel_id=str(channel["id"]),
            limit=limit,
            on_item=_on_item,
        )
    except ConnectorError as exc:
        return ToolResult(content=f"Couldn't read the channel: {exc}", summary="Teams read failed")
    _stash_virtual_file(ctx, txt_name, text)
    count = seen["total"]
    plural = "message" if count == 1 else "messages"
    excerpt = text[:_MESSAGES_EXCERPT_CHARS]
    _remember_messages(ctx, memo_key, {"count": count, "excerpt": excerpt})
    return ToolResult(
        content=(
            f"Read {count} {plural} from {team['name']} / #{channel['name']}. You now HAVE "
            f"the text below — do not read the channel again this turn.\n\n{excerpt}\n\n"
            f"{_MESSAGES_NOTE}"
        ),
        summary=f"Read #{channel['name']} · {count} {plural}",
    )


async def _create_docx(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Write a Word document (title + heading/body sections). Saves to the user's
    OneDrive when Word is connected, else hands back a 24h download link — the
    same dual path as create_slide_deck."""
    from sila.services.connectors.connector_service import ConnectorError, ConnectorService
    from sila.services.connectors.office_files import build_docx_sections

    title = str(args.get("title") or "").strip()[:120] or "Document"
    # One call, both formats. Asking for "a PDF and a Word doc" used to take two
    # prompts because there was no PDF path at all.
    also_pdf = bool(args.get("also_pdf"))
    raw_sections = args.get("sections") or []
    sections: list[tuple[str, str]] = []
    if isinstance(raw_sections, list):
        for s in raw_sections:
            if isinstance(s, dict):
                sections.append((str(s.get("heading") or ""), str(s.get("body") or "")))
    if not sections:
        plain = str(args.get("text") or "").strip()
        if not plain:
            return ToolResult(content="No content was provided for the document.")
        sections = [("", plain)]

    ctx.report(f"Writing '{title}'…")
    doc = build_docx_sections(title, sections)
    safe = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "-").lower() or "document"
    filename = f"{safe}.docx"
    ctx.report("Saving the document…")
    try:
        resp = await ConnectorService(ctx.db).execute_proxy(
            user_id=ctx.user.id,
            workspace_id=ctx.workspace_id,
            provider="ms-word",
            method="PUT",
            endpoint="word/upload",
            data={"title": title, "filename": filename, "__docx_bytes__": doc},
        )
    except ConnectorError:
        # No OneDrive/Word connection — store it and hand back a 24h link.
        from sila.db.repositories.generated_files import GeneratedFileRepository

        row = await GeneratedFileRepository(ctx.db).create(
            workspace_id=ctx.workspace_id,
            user_id=ctx.user.id,
            filename=filename,
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            file_bytes=doc,
        )
        # The capability URL must be LIVE before the user clicks it — the WS
        # session commits lazily, and an uncommitted row 404s (live bug: pptx).
        await ctx.db.commit()
        dl = f"{settings.sila_api_public_url.rstrip('/')}/v1/files/{row.id}"
        from sila.services.sila.deliverable_guard import (
            describe_for_model,
            guard_chunks,
            section_chunks,
        )

        security = await guard_chunks(section_chunks(title, sections))
        gate_note = describe_for_model(security, artifact=title)
        return ToolResult(
            content=(
                f"Wrote '{title}' ({len(sections)} sections). It is ATTACHED to your "
                "reply as a download card — do NOT paste a download link or a URL in "
                "your text; the card is the download. Say the document is ready, "
                "describe what is in it, and mention they can connect Word on the "
                "Connectors page to save future documents to OneDrive automatically "
                "(the link is valid 24h)." + (f"\n\n{gate_note}" if gate_note else "")
            ),
            summary=(
                f"Blocked by the security gateway: {title}"
                if security["status"] == "blocked"
                else f"Document ready: {title}"
            )[:80],
            meta={
                "artifacts": [
                    _artifact("doc", title, url=dl, subtitle="Word · .docx", security=security)
                ]
                + await _pdf_sibling(ctx, doc, filename, title, wanted=also_pdf, security=security)
            },
        )
    web_url = str(resp.get("webUrl", "")) if isinstance(resp, dict) else ""
    link = (
        f" Open it here: {web_url} — give the user the link as [the document](url)."
        if web_url
        else ""
    )
    return ToolResult(
        content=(
            f"Wrote '{title}' ({len(sections)} sections) and saved {filename} to OneDrive.{link}"
        ),
        summary=f"Document: {title}"[:80],
    )


_MAX_TASKS = 12
_TASK_CHARS = 90


# A stage label the user's own phrasing invites and the model then copies:
# "Phase 1 — Input: read the inbox" or "Step 2: chart it". The ORDER is the
# information; the label is scaffolding, and "Phase 1 — Input" as a checklist
# row tells the reader nothing about what is happening. Stripped here so a
# prompt the model half-follows still produces a readable plan.
_STAGE_LABEL = re.compile(
    r"^\s*(?:phase|step|stage|part)?\s*\d*\s*[—–\-:.)]*\s*"
    r"(?:input|processing|process|output|gathering|requirements(?:\s+and\s+data\s+gathering)?)"
    r"\s*[—–\-:.)]+\s*",
    re.IGNORECASE,
)
# An ordinal prefix: "Step 3 — ", "Phase 2: ", or a bare "1. ". The checklist
# already shows order and position, so the number is noise twice over.
_ORDINAL_PREFIX = re.compile(
    r"^\s*(?:(?:phase|step|stage|part)\s*)?\d+\s*[—–\-:.)]+\s*", re.IGNORECASE
)


def _task_title(raw: str) -> str:
    """A checklist row that names the work, with any stage label stripped.

    Strips a PREFIX only, and only when real work is left behind it. A title
    that is nothing BUT a label ("Phase 3 — Output") is returned unchanged:
    there is no work in it to keep, and rewriting it to "Output" would be no
    clearer while breaking the `done` matching, which compares the titles the
    model sent. The prompt is what stops those being sent; this is the net.
    """
    title = " ".join(raw.split())
    for pattern in (_STAGE_LABEL, _ORDINAL_PREFIX):
        stripped = pattern.sub("", title, count=1).strip(" —–-:.")
        # Two words or more, or it was never a label with work after it.
        if stripped and stripped != title and len(stripped.split()) >= 2:
            title = stripped
    return title


async def _track_tasks(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Publish the turn's task list, and mark off what is finished.

    A long request ("search, then table it, then chart it, then push a
    dashboard") is where a model loses the tail of the instruction. Holding the
    list as DATA the UI renders — and re-stating it after each step — is what
    keeps the last item from being quietly dropped.
    """
    raw = args.get("tasks")
    if not isinstance(raw, list) or not raw:
        return ToolResult(content="A plan needs at least one task.", summary="No tasks")
    done_raw = args.get("done")
    # `done` carries titles the model sends back, and it sends back whatever it
    # first wrote — including the stage label stripped from the published row.
    # Both sides are cleaned the same way or the step just finished never ticks.
    done = (
        {_task_title(str(d)).lower() for d in done_raw if str(d).strip()}
        if isinstance(done_raw, list)
        else set()
    )

    # A tick is monotonic within the turn. The orchestrator ticks a row the
    # moment its tool finishes; the model, re-publishing the plan a round later
    # with a shorter `done` list, un-ticked the web search it had just run and
    # then re-ran the step. What a completed tool completed stays completed.
    prior_done = {
        str(t.get("title", "")).lower()
        for t in (ctx.build_state.get("tasks") or [])
        if isinstance(t, dict) and t.get("done")
    }
    tasks: list[dict[str, Any]] = []
    for item in raw[:_MAX_TASKS]:
        title = _task_title(str(item))[:_TASK_CHARS]
        if title:
            key = title.lower()
            tasks.append({"title": title, "done": key in done or key in prior_done})
    if not tasks:
        return ToolResult(content="A plan needs at least one task.", summary="No tasks")
    if not prior_done and not done:
        # A fresh plan starts a fresh cadence: nothing is owed from a prior turn.
        ctx.build_state.pop("owe_feedback", None)
        ctx.build_state.pop("feedback_strikes", None)

    remaining = [t["title"] for t in tasks if not t["done"]]
    ctx.build_state["tasks"] = tasks
    note = (
        "All tasks are done — now write the final answer."
        if not remaining
        else "STILL TO DO, in order: "
        + "; ".join(remaining)
        + ". Do the next one, then call track_tasks again with it added to `done`."
    )
    return ToolResult(
        content=f"Plan updated ({len(tasks) - len(remaining)}/{len(tasks)} done). {note}",
        summary=f"{len(tasks) - len(remaining)}/{len(tasks)} done",
        meta={"tasks": tasks},
    )


async def _create_grafana_dashboard(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Create a dashboard on the workspace's connected Grafana from a title + a list
    of panels (each: title, type, query). Returns a link to the new dashboard."""
    from sila.adapters.connectors.grafana_client import GrafanaError
    from sila.services.connectors.grafana_service import GrafanaConfigError, GrafanaService

    title = str(args.get("title", "")).strip()
    panels = args.get("panels")
    if not title:
        return ToolResult(content="The dashboard needs a title.")

    # An uploaded data file wins: EMBED its data in the panels (TestData
    # csv_content) so the dashboard shows real charts — a spec of queries
    # against an empty datasource would render "No data" (the live failure).
    data_files = [
        f
        for f in (ctx.build_state.get("uploaded_files") or [])
        if isinstance(f, dict) and str(f.get("name", "")).lower().endswith((".csv", ".txt"))
    ]
    # Data the model produced itself is just as real as an upload. Without this
    # the only way to chart generated figures was to hand the user a CSV, ask
    # them to save it, and ask them to attach it back — a round trip through the
    # user for data the assistant already had in hand.
    inline_csv = str(args.get("csv_data", "") or "").strip()
    if not data_files and inline_csv:
        safe = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "-").lower() or "data"
        data_files = [{"name": f"{safe}.csv", "content": inline_csv}]
    if data_files:
        chosen = _pick_data_file(ctx, data_files)
        fname = str(chosen.get("name", "data.csv"))
        gate = await _template_propose_gate(ctx, chosen, deliverable="Grafana dashboard")
        if gate is not None:
            return gate
        ctx.report(f"Reading {fname}…")
        extra = _merge_chart_edits(ctx, args)
        # Continuity beats the model's title whims: once a dashboard exists for
        # this session, keep updating THAT one under its original name.
        if ctx.build_state.get("grafana_dash_uid") and ctx.build_state.get("grafana_dash_title"):
            title = str(ctx.build_state["grafana_dash_title"])
        try:
            spec = await _spec_for_chosen(
                ctx, chosen, focus=str(args.get("focus", "")) or None, extra_charts=extra
            )
        except ValueError as exc:
            return ToolResult(content=f"Couldn't chart {fname}: {exc}", summary="Unusable data")
        spec["charts"] = _prune_charts(ctx, spec["charts"])
        _remember_dashboard(ctx, spec)
        data_svc = GrafanaService(ctx.db)
        stored_uid = str(ctx.build_state.get("grafana_dash_uid") or "") or None

        async def _push(uid: str | None, dash_title: str) -> Any:
            return await data_svc.create_dashboard_with_data(
                workspace_id=ctx.workspace_id,
                title=dash_title,
                charts=spec["charts"],
                progress=ctx.report,
                # Same-session rebuilds UPDATE the same dashboard ("add a chart to
                # the same dashboard") instead of stacking near-duplicates.
                uid=uid,
            )

        def _name_conflict(exc: Exception) -> bool:
            msg = str(exc).lower()
            return "same name" in msg or "name-exists" in msg or "already exists" in msg

        def _suffixed(t: str) -> str:
            # Earlier sessions leave same-titled dashboards behind — a timestamp
            # suffix makes the create deterministic instead of a dead end.
            from datetime import datetime

            return f"{t} · {datetime.now().strftime('%b %d %H:%M')}"

        try:
            try:
                result = await _push(stored_uid, title)
            except GrafanaError as exc:
                if stored_uid is not None:
                    # Continuity self-heal: the session's dashboard may be gone or
                    # version-conflicted — build a FRESH one instead of failing.
                    log.warning("grafana.update_failed_retry_fresh", uid=stored_uid, error=str(exc))
                    ctx.build_state.pop("grafana_dash_uid", None)
                    try:
                        result = await _push(None, title)
                    except GrafanaError as exc2:
                        if not _name_conflict(exc2):
                            raise
                        title = _suffixed(title)
                        log.warning("grafana.name_conflict_retitle", title=title)
                        result = await _push(None, title)
                elif _name_conflict(exc):
                    title = _suffixed(title)
                    log.warning("grafana.name_conflict_retitle", title=title)
                    result = await _push(None, title)
                else:
                    raise
        except GrafanaConfigError as exc:
            log.warning("grafana.not_configured", error=str(exc))
            return ToolResult(
                content=(
                    f"Grafana isn't set up for this workspace: {exc}. TELL THE USER this "
                    "plainly (don't hide it), and still deliver any OTHER outputs they asked "
                    "for in the same message."
                ),
                summary="Grafana not ready",
            )
        except GrafanaError as exc:
            log.warning("grafana.push_failed", error=str(exc))
            return ToolResult(
                content=(
                    f"Grafana failed: {exc}. TELL THE USER Grafana failed with this reason "
                    "(don't hide it), offer to retry, and still deliver any OTHER outputs "
                    "they asked for in the same message."
                ),
                summary="Grafana error",
            )
        ctx.build_state["grafana_dash_uid"] = result.uid
        ctx.build_state["grafana_dash_title"] = title
        panel_names = ", ".join(
            str(c.get("title", "")) for c in spec["charts"] if c.get("kind") != "stat"
        )
        # The dashboard is ATTACHED as a card, like the deck and the agent. A
        # pasted URL makes the reader hunt through prose for a link; a card sits
        # with the turn's other deliverables and opens on one click.
        open_url = data_svc.last_public_url or result.url
        return ToolResult(
            content=(
                f"Created '{title}' on Grafana with the data from {fname} embedded "
                f"({len(spec['charts'])} panels: {panel_names}). It is ATTACHED to "
                "your reply as a card — do NOT paste the URL in your text; the card "
                "IS the link and repeating it just clutters the message. "
                "STRUCTURE YOUR REPLY IN 2 PARTS: (1) 1-2 sentences on what it holds; "
                "(2) two short pointers on what to look at first in THIS data, plus an "
                "offer to add or change charts, mirror it to Power BI, or re-run it later."
                + ("" if data_svc.last_public_url else " Mention it needs Grafana access.")
            ),
            summary=f"Built data dashboard: {title}"[:80],
            meta={
                "artifacts": [
                    _artifact(
                        "grafana",
                        title,
                        url=open_url,
                        subtitle=f"Grafana · {len(spec['charts'])} panels",
                    )
                ]
            },
        )

    if not isinstance(panels, list) or not panels:
        return ToolResult(content="The dashboard needs at least one panel.")
    ctx.report("Checking your Grafana connection…")
    svc = GrafanaService(ctx.db)
    try:
        result = await svc.create_dashboard(
            workspace_id=ctx.workspace_id,
            title=title,
            panels=[p for p in panels if isinstance(p, dict)],
            datasource_uid=(str(args["datasource_uid"]) if args.get("datasource_uid") else None),
            progress=ctx.report,
        )
    except GrafanaConfigError as exc:
        return ToolResult(content=str(exc), summary="Grafana not connected")
    except ValueError as exc:
        return ToolResult(content=f"Couldn't build the dashboard: {exc}", summary="Bad spec")
    except GrafanaError as exc:
        return ToolResult(content=f"Grafana rejected it: {exc}", summary="Grafana error")
    attached = (
        f" Panels are wired to the '{svc.last_datasource_name}' datasource; they show data "
        "once its metrics match the panel queries."
        if svc.last_datasource_name
        else " No datasource was found to attach — the user should add one in Grafana, then "
        "edit the panels (or rebuild)."
    )
    return ToolResult(
        content=(
            f"Created the dashboard '{title}' on Grafana: {result.url}.{attached} "
            "Give the user the link as [the dashboard](url)."
        ),
        summary=f"Built dashboard: {title}"[:80],
    )


def _norm_title(value: Any) -> str:
    return str(value or "").strip().casefold()


def _title_removed(chart: dict[str, Any], removed: list[str]) -> bool:
    """EXACT normalized title match, plus the stat-suffix form ("class" removes
    "Class (avg)"). Deliberately NOT substring: "class" must never take out
    "Top rows by Class" (live bug: removing a table column nuked the table)."""
    t = _norm_title(chart.get("title") or chart.get("metric"))
    if not t:
        return False
    return any(t == r or t.startswith(f"{r} (") for r in removed)


def _remember_removals(ctx: ToolContext, args: dict[str, Any]) -> list[str]:
    """Collect `remove_charts` titles across turns (build_state) so a removal
    sticks on every later rebuild — including the auto-generated charts."""
    stored = ctx.build_state.get("dashboard_removed_titles")
    removed: list[str] = [str(t) for t in stored] if isinstance(stored, list) else []
    incoming = args.get("remove_charts") if isinstance(args.get("remove_charts"), list) else []
    for t in incoming:
        tt = _norm_title(t)
        if tt and tt not in removed:
            removed.append(tt)
    if removed:
        ctx.build_state["dashboard_removed_titles"] = removed
    return removed


def _merge_chart_edits(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Accumulate chart edits across turns. EDIT semantics (live bug: "change the
    line to a pie" kept BOTH): an incoming spec whose title matches a stored one
    REPLACES it in place; new titles append; `remove_charts` titles drop stored
    extras (and are remembered so `_prune_charts` drops auto charts too).
    Persisted via build_state (the runner writes it to the session row)."""
    stored_raw = ctx.build_state.get("dashboard_extra_charts")
    stored = [c for c in stored_raw if isinstance(c, dict)] if isinstance(stored_raw, list) else []
    incoming = args.get("charts") if isinstance(args.get("charts"), list) else []
    removed = _remember_removals(ctx, args)
    index = {_norm_title(c.get("title") or c.get("metric")): i for i, c in enumerate(stored)}
    for c in incoming:
        if not isinstance(c, dict):
            continue
        key = _norm_title(c.get("title") or c.get("metric"))
        if key and key in index:
            stored[index[key]] = c  # same title = the user changed this chart
        else:
            index[key] = len(stored)
            stored.append(c)
    if removed:
        stored = [c for c in stored if not _title_removed(c, removed)]
    ctx.build_state["dashboard_extra_charts"] = stored
    return stored or None


async def _remember(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Save one durable fact about the user to their cross-chat memory (per
    USER, capacity-capped — the composer blocks when full until they prune)."""
    from sila.db.repositories.sila import SilaRepository

    if ctx.build_state.get(CONFIDENTIAL_KEY):
        # An OKR conversation writes nothing to long-term memory: no file content,
        # no figure, no objective name — and no preference derived from them.
        return ToolResult(
            content=(
                "NOT SAVED — this conversation carries a confidential OKR sheet, so "
                "nothing from it is written to memory. Do not tell the user it was saved."
            ),
            summary="Not saved (confidential)",
        )
    fact = str(args.get("fact", "")).strip()[:400]
    if not fact:
        return ToolResult(content="Nothing to remember — pass the fact as `fact`.")
    row = await SilaRepository(ctx.db).add_memory(ctx.user.id, fact, cap=settings.sila_max_memories)
    if row is None:
        return ToolResult(
            content=(
                "Memory is FULL — tell the user their SILA memory reached its limit and "
                "they can free space under Account & Usage → Memory."
            ),
            summary="Memory full",
        )
    return ToolResult(content=f"Saved to memory: {fact}", summary="Remembered")


async def _set_objective(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Analysis Engine Phase 1 (guided mode): store what the analysis is FOR.
    The objective becomes the ranking signal for every later stage, so setting
    it INVALIDATES any held AnalysisPlan — the next build re-proposes against
    the new objective (the plan-hash gate enforces this in code). Session state
    only; length-capped; audience from a fixed enum."""
    from sila.services.analysis import plan as plan_mod

    objective = plan_mod.normalize_objective(args)
    if not objective["question"]:
        return ToolResult(
            content="No objective given — pass the business question as `question`.",
            summary="Missing question",
        )
    ctx.build_state["analysis_objective"] = objective
    ctx.build_state.pop("analysis_plan", None)  # re-plan against the new objective
    parts = [f"question: {objective['question']}"]
    if objective["decision"]:
        parts.append(f"informs: {objective['decision']}")
    parts.append(f"audience: {objective['audience']}")
    if objective["metrics"]:
        parts.append(f"metrics: {', '.join(objective['metrics'])}")
    return ToolResult(
        content=(
            "Objective stored (GROUND TRUTH — quote these values exactly): "
            f"{' · '.join(parts)}. Now call the build tool the user asked for — it will "
            "propose an ANALYSIS PLAN ranked around this objective for their approval."
        ),
        summary="Objective set",
    )


def _latest_data_file(ctx: ToolContext, wanted: str = "") -> dict[str, Any] | None:
    """The data file the user means: csv/tsv/txt entries from the stash, resolved
    by the same rules every other data tool uses (_pick_data_file).

    It used to take the most recent, which is how `analyze_data` ended up on a
    mailbox export — no numeric columns, so it refused, three times in a row,
    while the spreadsheet the user attached sat in the same stash.
    """
    data_files = [
        f
        for f in (ctx.build_state.get("uploaded_files") or [])
        if isinstance(f, dict) and str(f.get("name", "")).lower().endswith((".csv", ".tsv", ".txt"))
    ]
    if not data_files:
        return None
    return _pick_data_file(ctx, data_files, wanted)


async def _analysis_path_for_chosen(ctx: ToolContext, chosen: dict[str, Any]) -> tuple[str, bool]:
    """(path, is_temp) for DuckDB analysis of whatever the source is. Datasets
    resolve to the owner-checked file (cleaned copy preferred); attachments
    are written to a temp file the CALLER must remove when is_temp."""
    import tempfile

    ds_id = str(chosen.get("dataset_id") or "").strip()
    if ds_id:
        from sila.db.repositories.sila import SilaRepository

        row = await SilaRepository(ctx.db).get_dataset(uuid.UUID(ds_id), ctx.user.id)
        if row is None:
            raise ValueError("that dataset isn't available anymore — ask the user to re-upload")
        cleaned = (ctx.build_state.get("cleaned_datasets") or {}).get(ds_id)
        return str(cleaned or row.path), False
    import os

    fd, tmp_path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8") as tmp:
        tmp.write(str(chosen.get("content", "")))
    return tmp_path, True


# Rows mirrored into Power BI: chat and Power BI must aggregate the SAME rows,
# so this matches the biggest dataset we comfortably push (20 chunked POSTs);
# beyond it the honest sample note appears.
_PBIX_MAX_ROWS = 200_000

_NO_DATA_FILE = ToolResult(
    content=(
        "No data file is attached. Ask the user to attach a CSV file "
        "(the paperclip in the composer) and ask again."
    ),
    summary="No data file",
)


async def _propose_cleaning(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Analysis Engine Phase 2: profile quality over the FULL file and return
    the deterministic CleaningRecipe as a proposal — nothing is applied until
    the user chooses (apply_cleaning refuses without this proposal)."""
    import os

    from sila.services.analysis import cleaning, quality

    chosen = _latest_data_file(ctx, str(args.get("file", "")).strip().lower())
    if chosen is None:
        return _NO_DATA_FILE
    name = str(chosen.get("name", "data.csv"))
    ctx.report(f"Scanning {name} for quality issues…")
    try:
        path, is_temp = await _analysis_path_for_chosen(ctx, chosen)
    except ValueError as exc:
        return ToolResult(content=f"Couldn't scan {name}: {exc}", summary="Unusable data")
    try:
        report = quality.quality_report(path)
    except ValueError as exc:
        return ToolResult(content=f"Couldn't scan {name}: {exc}", summary="Unusable data")
    finally:
        if is_temp:
            with contextlib.suppress(OSError):
                os.remove(path)
    recipe = cleaning.propose_recipe(report)
    ctx.build_state["quality_summary"] = {
        "score": report["score"],
        "deductions": report["deductions"][:3],
    }
    deductions = "; ".join(f"{d['reason']} (−{d['points']})" for d in report["deductions"][:3])
    if not recipe:
        ctx.build_state.pop("proposed_cleaning", None)
        return ToolResult(
            content=(
                f"GROUND TRUTH — quality of {name}: score {report['score']}/100. "
                + (
                    f"Remaining deductions (state them so the score is explained): {deductions}. "
                    if deductions
                    else "No deductions at all. "
                )
                + "No automatic fixes needed — tell the user the data looks clean "
                "and why any points are still missing."
            ),
            summary=f"Quality {report['score']}/100 — clean",
        )
    ctx.build_state["proposed_cleaning"] = {"file": name, "ops": recipe}
    fixes = "\n".join(f"  {i}. {cleaning.describe_op(op)}" for i, op in enumerate(recipe, start=1))
    return ToolResult(
        content=(
            f"GROUND TRUTH — quality of {name}: score {report['score']}/100 "
            f"(biggest issues: {deductions}). {len(recipe)} fixes proposed:\n{fixes}\n"
            "Relay this in plain words and ask: 'Apply all, pick some, or skip?' "
            "Then call apply_cleaning with the numbers they chose (omit `ops` for "
            "all). NOTHING is applied until they answer."
        ),
        summary=f"Quality {report['score']}/100 — {len(recipe)} fixes proposed",
    )


async def _apply_cleaning(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Apply ONLY user-approved ops from the standing proposal (plus explicit
    typed extras like drop_column/impute). Materializes a cleaned COPY — the
    original is untouched (undo_cleaning reverts), the applied recipe is kept
    for replay, and the AnalysisPlan is invalidated so the next build
    re-proposes on the cleaned data."""
    import os

    from sila.services.analysis import cleaning
    from sila.services.analysis.state import get_session

    chosen = _latest_data_file(ctx, str(args.get("file", "")).strip().lower())
    if chosen is None:
        return _NO_DATA_FILE
    name = str(chosen.get("name", "data.csv"))
    proposal = ctx.build_state.get("proposed_cleaning")
    if not isinstance(proposal, dict) or proposal.get("file") != name:
        return ToolResult(
            content=(
                f"No cleaning proposal exists for {name} — call propose_cleaning "
                "first and let the user choose. Nothing was applied."
            ),
            summary="Propose first",
        )
    recipe_all = list(proposal.get("ops") or [])
    picked = args.get("ops")
    if isinstance(picked, list) and picked:
        try:
            indexes = sorted({int(i) for i in picked})
            selected = [recipe_all[i - 1] for i in indexes if 1 <= i <= len(recipe_all)]
        except (TypeError, ValueError):
            return ToolResult(content="`ops` must be the fix numbers, e.g. [1, 3].")
        if len(selected) != len(indexes):
            return ToolResult(
                content=f"Fix numbers must be between 1 and {len(recipe_all)}. Nothing applied."
            )
    else:
        selected = recipe_all
    extras_raw = args.get("extra_ops") or []
    extras = [
        op
        for op in extras_raw
        if isinstance(op, dict) and str(op.get("op")) in cleaning.EXPLICIT_ONLY_OPS
    ]
    if len(extras) != len(extras_raw):
        return ToolResult(
            content=(
                "extra_ops may only contain the explicit ops: "
                f"{', '.join(cleaning.EXPLICIT_ONLY_OPS)}. Nothing applied."
            ),
            summary="Invalid extra op",
        )
    ops = selected + extras
    if not ops:
        return ToolResult(content="No ops chosen — nothing applied.")
    ctx.report(f"Cleaning {name}…")
    ds_id = str(chosen.get("dataset_id") or "").strip()
    try:
        if ds_id:
            src, _ = await _analysis_path_for_chosen(ctx, chosen)
            dst = os.path.join(settings.sila_datasets_dir, f"{ds_id}.cleaned.csv")
            tmp = f"{dst}.tmp"
            effects = cleaning.apply_recipe_to_file(src, ops, tmp)
            os.replace(tmp, dst)
            ctx.build_state.setdefault("cleaned_datasets", {})[ds_id] = dst
            get_session(ds_id).provenance.record(
                artifact=f"cleaning:{name}",
                source=name,
                operations=[cleaning.describe_op(op) for op in ops],
            )
        else:
            raw = str(chosen.get("content", ""))
            cleaned_text, effects = cleaning.apply_recipe_to_text(raw, ops)
            chosen.setdefault("content_raw", raw)
            chosen["content"] = cleaned_text
    except ValueError as exc:
        return ToolResult(content=f"Couldn't clean {name}: {exc}", summary="Cleaning failed")
    log_all = ctx.build_state.setdefault("cleaning_recipe", {})
    log_all[name] = (log_all.get(name) or []) + ops
    ctx.build_state.pop("analysis_plan", None)  # builds must re-plan on cleaned data
    lines = "\n".join(
        f"  - {e['describe']}"
        + (f" — {e['changed']:,} values touched" if e.get("changed") is not None else "")
        for e in effects["ops"]
    )
    caveats = "".join(f"\nCAVEAT (relay it): {c}." for c in effects.get("caveats") or [])
    return ToolResult(
        content=(
            f"GROUND TRUTH — cleaned {name}: {effects['rows_before']:,} → "
            f"{effects['rows_after']:,} rows.\n{lines}{caveats}\n"
            "Relay exactly these numbers. The next dashboard build will re-propose "
            "its plan on the cleaned data (undo_cleaning reverts everything)."
        ),
        summary=f"Cleaned {name} ({len(ops)} fixes)",
    )


async def _undo_cleaning(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Revert to the ORIGINAL data (the cleaned copy is forgotten/removed) and
    drop the applied recipe. Loud no-op when nothing was cleaned."""
    import os

    chosen = _latest_data_file(ctx, str(args.get("file", "")).strip().lower())
    if chosen is None:
        return _NO_DATA_FILE
    name = str(chosen.get("name", "data.csv"))
    ds_id = str(chosen.get("dataset_id") or "").strip()
    reverted = False
    if ds_id:
        cleaned = (ctx.build_state.get("cleaned_datasets") or {}).pop(ds_id, None)
        if cleaned:
            with contextlib.suppress(OSError):
                os.remove(str(cleaned))
            reverted = True
    elif "content_raw" in chosen:
        chosen["content"] = chosen.pop("content_raw")
        reverted = True
    if not reverted:
        return ToolResult(
            content=f"{name} was never cleaned — nothing to undo.", summary="Nothing to undo"
        )
    (ctx.build_state.get("cleaning_recipe") or {}).pop(name, None)
    ctx.build_state.pop("analysis_plan", None)
    return ToolResult(
        content=(
            f"Reverted {name} to its ORIGINAL uploaded data — all cleaning undone. "
            "The next build will re-propose its plan on the original."
        ),
        summary=f"Cleaning undone for {name}",
    )


_ANALYSIS_KINDS = (
    "compare",
    "correlate",
    "trend",
    "forecast",
    "drivers",
    "segments",
    "concentration",
)
_MAX_STORED_FINDINGS = 30
_MAX_EVIDENCE_POINTS = 300


def _trimmed_chart(chart: dict[str, Any]) -> dict[str, Any]:
    data = chart.get("data")
    if isinstance(data, list) and len(data) > 100:
        step = len(data) / 100
        chart = {**chart, "data": [data[int(i * step)] for i in range(100)]}
    return chart


def _remember_dashboard(ctx: ToolContext, spec: dict[str, Any]) -> None:
    """P5: the semantic layer, the exact stat figures, and a trimmed copy of
    the final chart list persist on the session — the report reproduces the
    dashboard's numbers from HERE, and explain_chart reads the same copy."""
    from sila.services.analysis import semantic

    charts = spec.get("charts") or []
    ctx.build_state["semantic_layer"] = semantic.layer_from_charts(charts)
    ctx.build_state["dashboard_stats"] = [
        {"title": c.get("title"), "value": c.get("value"), "sub": c.get("sub")}
        for c in charts
        if isinstance(c, dict) and c.get("kind") == "stat"
    ]
    ctx.build_state["last_dashboard"] = {
        "source": spec.get("source"),
        "rows": spec.get("rows"),
        "charts": [_trimmed_chart(c) for c in charts if isinstance(c, dict)],
    }


def _trimmed_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Findings persist on the session row — stride-sample big evidence series
    so the metadata JSON stays small."""
    evidence = finding.get("evidence")
    if isinstance(evidence, dict) and len(evidence.get("data") or []) > _MAX_EVIDENCE_POINTS:
        data = evidence["data"]
        step = len(data) / _MAX_EVIDENCE_POINTS
        finding = {
            **finding,
            "evidence": {
                **evidence,
                "data": [data[int(i * step)] for i in range(_MAX_EVIDENCE_POINTS)],
            },
        }
    return finding


async def _analyze_data(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Analysis Engine Phase 3: run ONE typed analysis over the FULL file and
    return ground-truth Findings. The math runs in code (DuckDB + stats); the
    model may only quote the statement slots and rendered phrasing — and the
    word 'significant' only ever appears when the guardrails allow it."""
    import os

    from sila.services.analysis import analyze, engine, temporal

    kind = str(args.get("analysis", "")).strip().lower()
    if kind not in _ANALYSIS_KINDS:
        return ToolResult(
            content=f"`analysis` must be one of: {', '.join(_ANALYSIS_KINDS)}.",
            summary="Unknown analysis",
        )
    chosen = _latest_data_file(ctx, str(args.get("file", "")).strip().lower())
    if chosen is None:
        return _NO_DATA_FILE
    name = str(chosen.get("name", "data.csv"))
    ctx.report(f"Analyzing {name}…")
    try:
        path, is_temp = await _analysis_path_for_chosen(ctx, chosen)
    except ValueError as exc:
        return ToolResult(content=f"Couldn't analyze {name}: {exc}", summary="Unusable data")
    try:
        prof = engine.profile_dataset(path)
        names = {c["name"].lower(): c["name"] for c in prof["columns"]}

        def resolve(arg_key: str, default_kind: str | None = None) -> str | None:
            wanted = str(args.get(arg_key, "")).strip().lower()
            if wanted:
                hit = names.get(wanted) or next((v for k, v in names.items() if wanted in k), None)
                if hit:
                    return hit
            if default_kind:
                return next((c["name"] for c in prof["columns"] if c["kind"] == default_kind), None)
            return None

        metric = resolve("metric", "number")
        by = resolve("by", "category")
        date_col = resolve("date", "date")
        objective = ctx.build_state.get("analysis_objective")
        if metric is None and kind != "correlate":
            raise ValueError("no numeric column found — name one via `metric`")
        if kind in ("compare", "concentration") and by is None:
            raise ValueError("no category column found — name one via `by`")
        if kind in ("trend", "forecast") and date_col is None:
            raise ValueError("no date column found — name one via `date`")
        if kind == "compare":
            segment = resolve("segment") or next(
                (c["name"] for c in prof["columns"] if c["kind"] == "category" and c["name"] != by),
                None,
            )
            found = analyze.compare_groups(path, metric, by, segment, objective)
        elif kind == "correlate":
            found = analyze.correlations(path, objective)
        elif kind == "concentration":
            found = analyze.concentration(path, metric, by, objective)
        elif kind == "drivers":
            found = analyze.drivers(path, resolve("target") or metric, objective)
        elif kind == "segments":
            found = analyze.segments(path, metric, objective)
        elif kind == "trend":
            found = temporal.trend_findings(path, metric, date_col, objective)
        else:  # forecast
            found = [temporal.forecast_finding(path, metric, date_col, objective)]
    except ValueError as exc:
        # Loud refusals included (e.g. forecast without 2 cycles) — relay as-is.
        return ToolResult(
            content=f"Couldn't run {kind} on {name}: {exc}", summary="Analysis refused"
        )
    finally:
        if is_temp:
            with contextlib.suppress(OSError):
                os.remove(path)
    stored = ctx.build_state.setdefault("findings", [])
    stored += [_trimmed_finding(f.to_dict()) for f in found]
    del stored[:-_MAX_STORED_FINDINGS]
    lines: list[str] = []
    for i, f in enumerate(found, start=1):
        lines.append(f"{i}. [{f.kind} · {f.confidence} confidence] {f.phrasing}")
        lines += [f"   CAVEAT (must relay): {c}" for c in f.caveats]
    return ToolResult(
        content=(
            "GROUND TRUTH FINDINGS — quote these values EXACTLY; mention no other "
            "numbers; never use the word 'significant' unless it already appears "
            "below:\n" + "\n".join(lines)
        ),
        summary=f"{kind} on {name}: {len(found)} finding(s)",
    )


async def _create_report_docx(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """P5: the analyst's Word report — Objective → Data & Quality → Method →
    Findings → Recommendations → Dashboard figures → Appendix. Every number is
    lifted verbatim from session state computed in code, so the report
    reproduces the dashboard's figures exactly. Delivery = the create_docx
    dual path (OneDrive or a 24h download link)."""
    from sila.services.analysis.report import build_report_sections

    sections = build_report_sections(ctx.build_state)
    title = str(args.get("title", "")).strip()[:120] or (
        f"Analysis report — {(ctx.build_state.get('last_dashboard') or {}).get('source', 'data')}"
    )
    return await _create_docx(ctx, {"title": title, "sections": sections})


async def _create_insight_deck(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """P5: the exec deck — one insight per slide (what happened as the title,
    so-what + confidence + caveats as bullets), composed in code from the
    ranked insights and delivered through the existing slide-deck pipeline
    (workspace templates included)."""
    from sila.services.analysis.report import deck_slides

    insights = [i for i in (ctx.build_state.get("insights") or []) if isinstance(i, dict)]
    if not insights:
        return ToolResult(
            content=(
                "No ranked insights yet — run analyze_data and then rank_insights "
                "before building the deck."
            ),
            summary="No insights for the deck",
        )
    style = str(args.get("style", "")).strip().lower()
    if style not in ("executive", "technical"):
        # Gate in code: the audience decides the deck. Ask BEFORE building.
        return ToolResult(
            content=(
                "Before building, ask the user which kind of deck they want (and "
                "the template, in the SAME message so they answer once): "
                "'executive' — a management pitch: headline slides, plain language, "
                "one note per slide; or 'technical' — the full detail with "
                "confidence levels and every caveat. Then call this tool again "
                "with `style` set."
            ),
            summary="Deck style needed",
        )
    slides = deck_slides(insights, ctx.build_state.get("exec_summary"), style)
    title = str(args.get("title", "")).strip() or "Analysis insights"
    return await _create_slide_deck(
        ctx, {"title": title, "slides": slides, "template": args.get("template", "")}
    )


async def _explain_chart(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """P5: plain-language, bilingual (EN/AR) explanation of one chart from the
    LAST built dashboard — exact-title match (same semantics as chart edits).
    The explanation is rendered in code and number-guarded against the chart
    itself, so it cannot quote a figure the chart doesn't contain."""
    from sila.services.analysis.explain import explain_chart

    dash = ctx.build_state.get("last_dashboard") or {}
    charts = [c for c in (dash.get("charts") or []) if isinstance(c, dict)]
    if not charts:
        return ToolResult(
            content="No dashboard has been built yet — build one first, then ask again.",
            summary="Nothing to explain",
        )
    wanted = _norm_title(str(args.get("title", "")))
    chart = next((c for c in charts if _norm_title(c.get("title")) == wanted), None)
    if chart is None and wanted:
        chart = next((c for c in charts if wanted in _norm_title(c.get("title"))), None)
    if chart is None:
        titles = "; ".join(str(c.get("title")) for c in charts)
        return ToolResult(
            content=f"No chart titled like that. The dashboard has: {titles}.",
            summary="Chart not found",
        )
    try:
        text = explain_chart(chart)
    except ValueError as exc:
        return ToolResult(content=f"Couldn't explain safely: {exc}", summary="Explain refused")
    return ToolResult(
        content=(
            "GROUND TRUTH EXPLANATION — relay the one matching the user's language, "
            f"verbatim where numbers appear.\nEN: {text['en']}\nAR: {text['ar']}"
        ),
        summary=f"Explained '{chart.get('title')}'",
    )


async def _set_alert_rule(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """P6: typed alert thresholds ('tell me if churn > 5%') stored on the
    session and evaluated in code on every refresh. add / list / clear."""
    from sila.services.analysis import monitor

    rules: list[dict[str, Any]] = ctx.build_state.setdefault("alert_rules", [])
    action = str(args.get("action", "add")).strip().lower()
    if action == "clear":
        rules.clear()
        return ToolResult(content="All alert rules cleared.", summary="Alerts cleared")
    if action == "add":
        rule = monitor.normalize_rule(args)
        if rule is None:
            return ToolResult(
                content=(
                    "Invalid rule — pass metric (column name), cmp (gt/lt/ge/le), "
                    "threshold (number), and optionally agg (avg/sum/min/max/count)."
                ),
                summary="Invalid alert rule",
            )
        if rule not in rules:
            if len(rules) >= 10:
                return ToolResult(
                    content="Rule limit reached (10) — clear some first.",
                    summary="Too many rules",
                )
            rules.append(rule)
    listing = (
        "\n".join(f"  {i}. {monitor.describe_rule(r)}" for i, r in enumerate(rules, start=1))
        or "  (none)"
    )
    return ToolResult(
        content=(
            f"Alert rules now active (checked on every refresh):\n{listing}\n"
            "Relay this list; rules also travel with the user's own agent copy."
        ),
        summary=f"{len(rules)} alert rule(s)",
    )


async def _refresh_analysis(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """P6: re-run the known pipeline on the CURRENT data — replay the cleaning
    recipe, check drift against the baseline (a schema change REFUSES to
    rebuild until acknowledged — report before rebuilding), evaluate alert
    rules, then rebuild the dashboard through the normal path."""
    import os

    from sila.services.analysis import cleaning, monitor
    from sila.services.analysis import plan as plan_mod

    chosen = _latest_data_file(ctx, str(args.get("file", "")).strip().lower())
    if chosen is None:
        return _NO_DATA_FILE
    name = str(chosen.get("name", "data.csv"))
    ctx.report(f"Refreshing the pipeline on {name}…")

    # 1 — replay the stored cleaning recipe on FRESH data (loud on mismatch).
    recipe = (ctx.build_state.get("cleaning_recipe") or {}).get(name) or []
    replayed = False
    ds_id = str(chosen.get("dataset_id") or "").strip()
    try:
        if recipe and ds_id and ds_id not in (ctx.build_state.get("cleaned_datasets") or {}):
            src, _ = await _analysis_path_for_chosen(ctx, chosen)
            dst = os.path.join(settings.sila_datasets_dir, f"{ds_id}.cleaned.csv")
            tmp = f"{dst}.tmp"
            cleaning.apply_recipe_to_file(src, recipe, tmp)
            os.replace(tmp, dst)
            ctx.build_state.setdefault("cleaned_datasets", {})[ds_id] = dst
            replayed = True
        elif recipe and not ds_id and "content_raw" not in chosen:
            raw = str(chosen.get("content", ""))
            cleaned_text, _ = cleaning.apply_recipe_to_text(raw, recipe)
            chosen["content_raw"] = raw
            chosen["content"] = cleaned_text
            replayed = True
    except ValueError as exc:
        return ToolResult(
            content=(
                f"The saved cleaning recipe no longer fits {name}: {exc}. "
                "The schema likely changed — tell the user (plain words, no "
                "internal tool names) and offer a fresh data-quality scan."
            ),
            summary="Recipe replay failed",
        )

    # 2 — baseline + drift + alerts on the current (post-replay) data.
    try:
        path, is_temp = await _analysis_path_for_chosen(ctx, chosen)
    except ValueError as exc:
        return ToolResult(content=f"Couldn't refresh {name}: {exc}", summary="Unusable data")
    try:
        try:
            current = monitor.capture_baseline(path)
        except Exception as exc:  # noqa: BLE001 — unreadable file, loud
            return ToolResult(content=f"Couldn't profile {name}: {exc}", summary="Unusable data")
        baseline = ctx.build_state.get("monitor_baseline")
        drift = monitor.drift_report(baseline, current) if isinstance(baseline, dict) else None
        if drift and drift["schema_changed"] and not bool(args.get("acknowledge_drift")):
            # THE gate: report before rebuilding — never swallow a schema change.
            return ToolResult(
                content=(
                    "DRIFT DETECTED — the dashboard was NOT rebuilt.\n"
                    f"{monitor.describe_drift(drift)}\n"
                    "Relay this to the user and ask whether to rebuild on the new "
                    "schema. Only after their explicit yes, call refresh_analysis "
                    "again with acknowledge_drift=true."
                ),
                summary="Schema drift — rebuild blocked",
            )
        alert_results = monitor.evaluate_alerts(path, ctx.build_state.get("alert_rules") or [])
    finally:
        if is_temp:
            with contextlib.suppress(OSError):
                os.remove(path)
    ctx.build_state["monitor_baseline"] = current

    # 3 — an APPROVED plan stays approved across a refresh: same objective,
    # same charts, new rows — rebind the hash instead of re-gating the user.
    held = ctx.build_state.get("analysis_plan")
    if isinstance(held, dict) and held.get("approved"):
        try:
            preview = await _spec_for_chosen(ctx, chosen)
            objective = ctx.build_state.get("analysis_objective") or plan_mod.infer_objective(
                preview
            )
            held["hash"] = plan_mod.plan_hash(
                objective,
                str(preview.get("source", name)),
                int(preview.get("rows", 0)),
                [str(c.get("title", "")) for c in preview.get("charts", [])],
            )
        except ValueError:
            pass  # the build below will surface the real problem

    # 4 — rebuild through the normal path, then report drift + alerts LOUD.
    result = await _create_data_dashboard(ctx, {"file": name})
    ctx.build_state["monitor_config"] = monitor.export_monitor_config(ctx.build_state)
    lines = ["REFRESH REPORT (relay all of it):"]
    if replayed:
        lines.append(f"  Cleaning recipe replayed ({len(recipe)} fixes) on the fresh data.")
    lines.append(
        "  Baseline established — future refreshes compare against it."
        if drift is None
        else f"  Drift: {monitor.describe_drift(drift)}"
    )
    for entry in alert_results:
        rule_text = monitor.describe_rule(entry["rule"])
        if entry.get("error"):
            lines.append(f"  ALERT RULE BROKEN: {rule_text} — {entry['error']}")
        elif entry.get("triggered"):
            lines.append(f"  ALERT TRIGGERED: {rule_text} — actual value {entry['value']:g}")
        else:
            lines.append(f"  alert ok: {rule_text} (actual {entry['value']:g})")
    triggered = sum(1 for e in alert_results if e.get("triggered"))
    return ToolResult(
        content="\n".join(lines) + "\n\n" + result.content,
        summary=f"Refreshed {name}" + (f" — {triggered} alert(s)" if triggered else ""),
        ui=result.ui,
    )


def _pdd_state(ctx: ToolContext) -> dict[str, Any] | None:
    state = ctx.build_state.get("pdd")
    return state if isinstance(state, dict) and state.get("source") else None


def _pdd_quality_lines(q: dict[str, Any]) -> str:
    lines = [
        f"QUALITY REPORT — Coverage {q['coverage_pct']}% · Groundedness "
        f"{q['groundedness_pct']}% · {q['open_items']} open item(s) · "
        f"{q['extracted_kept']} grounded fact(s) kept, {q['extracted_dropped']} deleted "
        "as ungroundable."
    ]
    if q.get("drop_reasons"):
        lines.append(
            "  Deleted because: "
            + "; ".join(f"{d['count']}× {d['reason']}" for d in q["drop_reasons"])
        )
    if q.get("sections_at_risk"):
        lines.append("  Sections at risk: " + ", ".join(q["sections_at_risk"]))
    for g in q.get("blocking_questions") or []:
        lines.append(f"  BLOCKING {g['id']} (§{g['section']}): {g['question']}")
        if g.get("suggestion"):
            lines.append(f"    suggestion: {g['suggestion']}")
    for g in (q.get("other_questions") or [])[:5]:
        lines.append(f"  {g['id']} (§{g['section']}): {g['question']}")
    return "\n".join(lines)


async def _pdd_start(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """PDD template: ingest the attached transcript — roster and chunking are
    pure code; nothing model-derived enters here."""
    from sila.services.pdd import session as pdd_session

    wanted = str(args.get("file", "")).strip().lower()
    transcripts = [
        f
        for f in (ctx.build_state.get("uploaded_files") or [])
        if isinstance(f, dict)
        and str(f.get("name", "")).lower().endswith((".txt", ".md", ".vtt"))
        and str(f.get("content") or "").strip()
    ]
    if wanted:
        named = [f for f in transcripts if wanted in str(f.get("name", "")).lower()]
        transcripts = named or transcripts
    if not transcripts:
        return ToolResult(
            content=(
                "No transcript is attached — ask the user to attach the meeting "
                "transcript as a .txt or .md file (the paperclip in the composer)."
            ),
            summary="No transcript",
        )
    chosen = transcripts[-1]
    existing = ctx.build_state.get("pdd")
    if isinstance(existing, dict) and existing.get("items") and not bool(args.get("reset")):
        return ToolResult(
            content=(
                "A PDD session with extracted content already exists — continue "
                "it (questions/edits/export). Only pass reset=true after the "
                "USER explicitly asks to start over; resetting discards "
                "everything extracted and answered."
            ),
            summary="Session already in progress",
        )
    try:
        state = pdd_session.start(
            doc_type=str(args.get("doc_type", "")).strip().lower(),
            transcript=str(chosen.get("content", "")),
            process_name=str(args.get("process_name", "")),
            department=str(args.get("department", "")),
        )
    except ValueError as exc:
        return ToolResult(content=f"Couldn't start: {exc}", summary="Ingest refused")
    ctx.build_state["pdd"] = state
    summary = pdd_session.ingest_summary(state)
    roster_lines = "\n".join(
        f"  {i + 1}. {sp['name']} — {sp['role']} ({sp['mentions']} contribution(s))"
        for i, sp in enumerate(summary["speakers"])
    )
    return ToolResult(
        content=(
            f"Ingested {chosen.get('name')} for a {summary['doc_type_label']}: "
            f"{summary['chars']:,} characters in {summary['chunks']} part(s). "
            "ROSTER (ground truth, from transcript structure — identities become "
            f"[Contact Name n] in the document):\n{roster_lines or '  (no speakers found)'}\n"
            "Relay the roster and ask the user to confirm before the deep read."
        ),
        summary=f"Transcript ingested ({len(summary['speakers'])} speakers)",
    )


async def _pdd_extract(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """One resumable extraction pass (one chunk × five section families).
    Call again while done=false. Finishing runs derivation + the gap
    register — the propose gate this template exports through."""
    del args
    from sila.adapters.llm.ollama import OllamaLLMAdapter
    from sila.services.pdd import session as pdd_session

    state = _pdd_state(ctx)
    if state is None:
        return ToolResult(
            content="No PDD session yet — ingest the transcript first.", summary="Not started"
        )
    gateway = OllamaLLMAdapter(base_url=settings.ollama_url, model=settings.sila_llm_model)
    out = await pdd_session.extract_next_chunk(state, gateway, ctx.report)
    if not out.get("done"):
        return ToolResult(
            content=(
                f"Read part {out['chunk']}/{out['chunks']} of the transcript. "
                "Call this tool again to continue (do not reply to the user yet)."
            ),
            summary=f"Extraction {out['chunk']}/{out['chunks']}",
        )
    return ToolResult(
        content=(
            "Extraction complete — relay this VERBATIM where numbers appear, then "
            "the blocking questions with their suggestions (the document cannot "
            "export around them):\n" + _pdd_quality_lines(out)
        ),
        summary=f"Extraction done — coverage {out['coverage_pct']}%",
    )


async def _pdd_answer_gap(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Apply the user's answer to one gap (or waive it) and report what is
    still owed. Answers become user_answered content — never invented."""
    from sila.services.pdd import session as pdd_session

    state = _pdd_state(ctx)
    if state is None:
        return ToolResult(content="No PDD session yet.", summary="Not started")
    gap_ref = str(args.get("gap_id", "")).strip()
    if bool(args.get("waive_all")):
        gap_ref = "all"
    try:
        q = pdd_session.answer_gap(
            state,
            gap_id=gap_ref,
            answer=str(args.get("answer", "")),
            waive=bool(args.get("waive")),
        )
    except (KeyError, ValueError) as exc:
        open_gaps = ", ".join(
            f"{g['id']}=§{g['section_id']}"
            for g in state.get("gaps") or []
            if g.get("status") == "open"
        )
        return ToolResult(
            content=(
                f"Couldn't apply that: {exc}. Reference a gap by its id or its "
                f"section number. Open gaps: {open_gaps or 'none'}."
            ),
            summary="Gap not applied",
        )
    return ToolResult(
        content="Recorded. Current state:\n" + _pdd_quality_lines(q),
        summary=f"{q['open_items']} open item(s) left",
    )


async def _pdd_edit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Typed patches against the structured document (never prose edits).
    Renaming steps syncs the RACI + figures automatically; batches are
    all-or-nothing."""
    from sila.services.pdd import session as pdd_session
    from sila.services.pdd.refine import PatchError

    state = _pdd_state(ctx)
    if state is None:
        return ToolResult(content="No PDD session yet.", summary="Not started")
    patches = args.get("patches") if isinstance(args.get("patches"), list) else []
    if not patches:
        return ToolResult(
            content=(
                "Pass `patches`: a list of typed ops — set_prose {section,text}, "
                "set_cell {section,row,column,value}, add_row {section,cells}, "
                "remove_row {section,row}, add_item/remove_item, set_meta {key,value}, "
                "set_raci {activity,person,letter}."
            ),
            summary="No patches",
        )
    try:
        changes = pdd_session.apply_edits(state, patches)
    except PatchError as exc:
        return ToolResult(content=f"Nothing was changed — {exc}", summary="Patch rejected")
    return ToolResult(
        content="Applied (relay exactly):\n" + "\n".join(f"  - {c}" for c in changes),
        summary=f"{len(changes)} change(s)",
    )


async def _pdd_export(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """The export boundary: quality report + gate + redaction + Word file.
    Blocked exports relay the remaining questions — never argue with the gate.
    waive_open=true (ONLY on an explicit skip/export-anyway ask) waives every
    remaining question first, so "skip and export" is one deterministic call."""
    from sila.services.pdd import session as pdd_session

    state = _pdd_state(ctx)
    if state is None:
        return ToolResult(content="No PDD session yet.", summary="Not started")
    if bool(args.get("waive_open")):
        pdd_session.answer_gap(state, gap_id="all")
    try:
        doc, filename, quality = pdd_session.export_bytes(state)
    except ValueError as exc:
        q = pdd_session.quality_report(state)
        return ToolResult(
            content=(
                f"EXPORT BLOCKED — {exc}\nRelay the remaining questions. If the "
                "user says to skip them / export anyway, call THIS tool again "
                "with waive_open=true — do not just retry.\n" + _pdd_quality_lines(q)
            ),
            summary="Export blocked by open gaps",
        )
    ctx.report("Rendering the document…")
    from sila.db.repositories.generated_files import GeneratedFileRepository

    row = await GeneratedFileRepository(ctx.db).create(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user.id,
        filename=filename,
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        file_bytes=doc,
    )
    await ctx.db.commit()  # the capability URL must be live before the click
    dl = f"{settings.sila_api_public_url.rstrip('/')}/v1/files/{row.id}"
    return ToolResult(
        content=(
            "Document exported and ATTACHED to your reply as a download card — do "
            "NOT paste a download link or a URL in your text; the card is the "
            "download. Relay the quality line FIRST, then say the document is "
            f"ready (the link is valid 24h):\n{_pdd_quality_lines(quality)}\n"
            "Identities are pseudonymized; the mapping stays with the document owner."
        ),
        summary=f"Exported {filename}",
        meta={"artifacts": [_artifact("doc", filename, url=dl, subtitle="Word · .docx")]},
    )


async def _rank_insights(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Analysis Engine Phase 4: rank the session's accumulated findings into
    insight cards + an executive summary. All prose is generated in code and
    number-guarded against the statement slots — the model relays, verbatim
    where numbers appear."""
    from sila.services.analysis import insights as insights_mod

    findings = [f for f in (ctx.build_state.get("findings") or []) if isinstance(f, dict)]
    if not findings:
        return ToolResult(
            content=(
                "No findings yet — run analyze_data (compare/trend/drivers/…) on the "
                "attached data first, then rank the insights."
            ),
            summary="No findings to rank",
        )
    try:
        top_k = max(1, min(10, int(args.get("top_k", 5))))
    except (TypeError, ValueError):
        top_k = 5
    cards = insights_mod.rank_insights(findings, top_k)
    summary = insights_mod.executive_summary(cards, ctx.build_state.get("analysis_objective"))
    ctx.build_state["insights"] = [c.to_dict() for c in cards]
    ctx.build_state["exec_summary"] = summary
    lines: list[str] = ["EXECUTIVE SUMMARY:"]
    if summary.get("headline"):
        lines.append(f"  Headline: {summary['headline']['label']} = {summary['headline']['value']}")
    lines += [f"  • {b}" for b in summary["bullets"]]
    lines.append("")
    for i, card in enumerate(cards, start=1):
        lines.append(f"{i}. [{card.kind} · {card.confidence} confidence · score {card.score:g}]")
        lines.append(f"   What happened: {card.what_happened}")
        lines.append(f"   So what: {card.so_what}")
        lines += [f"   CAVEAT (must relay): {c}" for c in card.caveats]
    return ToolResult(
        content=(
            "GROUND TRUTH INSIGHTS — relay the executive summary first, then the "
            "cards; quote numbers EXACTLY as written, include every CAVEAT, and add "
            "no numbers of your own:\n" + "\n".join(lines)
        ),
        summary=f"{len(cards)} insight(s) ranked",
    )


async def _spec_for_chosen(
    ctx: ToolContext,
    chosen: dict[str, Any],
    focus: str | None = None,
    extra_charts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One dashboard spec, whatever the source: a chat ATTACHMENT keeps the
    proven stdlib profiler (v1, byte-identical), an uploaded DATASET runs on
    DuckDB over the FULL file (Analysis Engine P0 — truncation is gone).
    Raises ValueError on unusable input (shared contract)."""
    from sila.services.sila.data_dashboard import build_data_dashboard

    name = str(chosen.get("name", "data.csv"))
    ds_id = str(chosen.get("dataset_id") or "").strip()
    if ds_id:
        from sila.db.repositories.sila import SilaRepository
        from sila.services.analysis import engine
        from sila.services.analysis.state import get_session

        row = await SilaRepository(ctx.db).get_dataset(uuid.UUID(ds_id), ctx.user.id)
        if row is None:
            raise ValueError("that dataset isn't available anymore — ask the user to re-upload")
        cleaned = (ctx.build_state.get("cleaned_datasets") or {}).get(ds_id)
        return engine.dashboard_from_dataset(
            str(cleaned or row.path),
            name,
            focus=focus,
            extra_charts=extra_charts,
            provenance=get_session(ds_id).provenance,
        )
    return build_data_dashboard(
        name, str(chosen.get("content", "")), focus=focus, extra_charts=extra_charts
    )


async def _dataset_csv_sample(ctx: ToolContext, chosen: dict[str, Any]) -> tuple[str, bool]:
    """(csv_text, sampled) for renderers that need raw rows. Attachments return
    their full (already small) text; datasets export a row-capped sample."""
    ds_id = str(chosen.get("dataset_id") or "").strip()
    if not ds_id:
        return str(chosen.get("content", "")), False
    from sila.db.repositories.sila import SilaRepository
    from sila.services.analysis import engine

    row = await SilaRepository(ctx.db).get_dataset(uuid.UUID(ds_id), ctx.user.id)
    if row is None:
        raise ValueError("that dataset isn't available anymore — ask the user to re-upload")
    cleaned = (ctx.build_state.get("cleaned_datasets") or {}).get(ds_id)
    return engine.export_csv_sample(str(cleaned or row.path), _PBIX_MAX_ROWS)


async def _template_propose_gate(
    ctx: ToolContext, chosen: dict[str, Any], deliverable: str = "in-chat dashboard"
) -> ToolResult | None:
    """The Phase-1 plan gate for TEMPLATE sessions: build tools REFUSE to run
    unless the session holds an APPROVED AnalysisPlan whose hash still matches
    the current objective + file identity + planned visuals. First build call
    → propose the plan (auto-inferred objective unless set_objective stored
    one) and wait; the follow-up call (after the user's turn) approves it and
    passes through. A re-upload (row count change), a new file, or a changed
    objective invalidates the hash → re-propose. Non-template sessions are
    untouched. O(rows) profile per call (the preview doubles as the check)."""
    if not ctx.build_state.get("active_template"):
        return None
    from sila.services.analysis import plan as plan_mod

    name = str(chosen.get("name", "data.csv"))
    try:
        preview = await _spec_for_chosen(ctx, chosen)
    except ValueError:
        return None  # unusable file → let the real builder surface its error
    objective = ctx.build_state.get("analysis_objective") or plan_mod.infer_objective(preview)
    current_hash = plan_mod.plan_hash(
        objective,
        str(preview.get("source", name)),
        int(preview.get("rows", 0)),
        [str(c.get("title", "")) for c in preview.get("charts", [])],
    )
    turn = int(ctx.build_state.get("turn_seq", 0))
    held = ctx.build_state.get("analysis_plan")
    if isinstance(held, dict) and held.get("hash") == current_hash:
        if held.get("approved"):
            return None  # one approval, then deterministic execution
        if int(held.get("proposed_turn", -1)) != turn:
            # The proposal round-tripped through the user's turn — approve, build.
            held["approved"] = True
            return None
        # Same turn as the proposal: the model may NOT approve its own plan.
        return ToolResult(
            content=(
                "STOP — the ANALYSIS PLAN above is still awaiting the user's answer. "
                "Relay it and WAIT; do not call build tools again this turn."
            ),
            summary="Awaiting plan approval",
        )
    new_plan = plan_mod.build_plan(objective, preview, deliverable)
    new_plan["proposed_turn"] = turn
    applied = (ctx.build_state.get("cleaning_recipe") or {}).get(name)
    pending = ctx.build_state.get("proposed_cleaning")
    if applied:
        new_plan["cleaning"] = f"{len(applied)} fixes applied"
    elif isinstance(pending, dict) and pending.get("file") == name and pending.get("ops"):
        new_plan["cleaning"] = f"{len(pending['ops'])} fixes proposed — not applied yet"
    ctx.build_state["analysis_plan"] = new_plan
    cols = ", ".join(f"{c['name']}" for c in preview["columns"][:10])
    return ToolResult(
        content=(
            "PROPOSE FIRST — do NOT build yet. Relay this ANALYSIS PLAN to the user in "
            "plain, friendly words (the values are ground truth — quote them exactly):\n\n"
            f"{plan_mod.render_plan(new_plan)}\n\n"
            f"Key columns: {cols}. If the inferred objective looks off, ask what the "
            "analysis is FOR and call set_objective with their answer. Otherwise end by "
            "asking 'Shall I build it like that, or change something?' and WAIT. When "
            "they agree or adjust, call this tool again (their adjustments go in "
            "`charts` / `remove_charts`)."
        ),
        summary=f"Proposed a plan for {name}",
    )


def _recipe_identity(chart: dict[str, Any]) -> tuple[Any, ...] | None:
    """What a chart MEANS, independent of its title — the model re-sends the
    plan's visuals with re-cased/re-worded titles ('revenue by region' →
    'Total Revenue by Region'), and different-title-means-add stacked
    near-duplicates (live bug). Pies collapse per dimension regardless of
    metric: two pies over the same `by` are redundant to a reader."""
    kind = str(chart.get("kind", ""))
    if not kind or kind == "table":
        return None  # tables may legitimately differ only by columns/limit
    if kind == "pie":
        return ("pie", str(chart.get("by") or ""))
    return (
        kind,
        str(chart.get("metric") or ""),
        str(chart.get("by") or ""),
        str(chart.get("agg") or ""),
        tuple(str(s) for s in chart.get("series_cols") or []),
        tuple(str(s) for s in chart.get("columns") or []) if kind == "radar" else (),
    )


def _prune_charts(ctx: ToolContext, charts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Final pass over the BUILT chart list: per title the LAST spec wins in the
    FIRST spec's position (an edited chart replaces the auto-built version
    instead of stacking a duplicate — which also broke React keys); charts with
    the same RECIPE identity but a re-worded title collapse the same way; and
    remembered removals drop auto charts too. O(charts)."""
    by_key: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    seen_recipes: dict[tuple[Any, ...], Any] = {}
    for i, c in enumerate(charts):
        key: Any = _norm_title(c.get("title")) or f"__untitled_{i}"
        recipe = _recipe_identity(c)
        if recipe is not None:
            if recipe in seen_recipes:
                key = seen_recipes[recipe]  # same meaning → replace, don't stack
            else:
                seen_recipes[recipe] = key
        if key not in by_key:
            order.append(key)
        by_key[key] = c
    out = [by_key[k] for k in order]
    stored = ctx.build_state.get("dashboard_removed_titles")
    removed = [str(t) for t in stored] if isinstance(stored, list) else []
    if removed:
        out = [c for c in out if not _title_removed(c, removed)]
    return out


async def _create_data_dashboard(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Build an in-Sila dashboard from a data file the user attached (CSV). All
    parsing/aggregation is deterministic code; the model only names an optional
    focus column. Renders inline in the chat — the data never leaves the box."""

    files = ctx.build_state.get("uploaded_files") or []
    wanted = str(args.get("file", "")).strip().lower()
    data_files = [
        f
        for f in files
        if isinstance(f, dict) and str(f.get("name", "")).lower().endswith((".csv", ".txt"))
    ]
    # Figures the model gathered or modelled itself are just as real as an
    # upload — the same escape hatch create_grafana_dashboard has. Without it a
    # "search the web, then chart it here" turn dead-ends by handing the user a
    # CSV and asking them to attach it back, for data already in hand.
    inline_csv = str(args.get("csv_data", "") or "").strip()
    if not data_files and inline_csv:
        raw = str(args.get("title", "")).strip() or "data"
        safe = re.sub(r"[^\w\- ]+", "", raw).strip().replace(" ", "-").lower() or "data"
        data_files = [{"name": f"{safe}.csv", "content": inline_csv}]
    if not data_files:
        return ToolResult(
            content=(
                "No data file is attached and no `csv_data` was passed. If you "
                "produced the figures yourself, resend them as `csv_data`; "
                "otherwise ask the user to attach a CSV (the paperclip)."
            ),
            summary="No data file",
        )
    chosen = _pick_data_file(ctx, data_files, wanted)
    name = str(chosen.get("name", "data.csv"))
    # Remembered so the Grafana push and the report follow THIS dataset.
    ctx.build_state["dashboard_source"] = name
    gate = await _template_propose_gate(ctx, chosen)
    if gate is not None:
        return gate
    ctx.report(f"Reading {name}…")
    extra = _merge_chart_edits(ctx, args)
    try:
        dashboard = await _spec_for_chosen(
            ctx, chosen, focus=str(args.get("focus", "")) or None, extra_charts=extra
        )
    except ValueError as exc:
        return ToolResult(content=f"Couldn't chart {name}: {exc}", summary="Unusable data")
    dashboard["charts"] = _prune_charts(ctx, dashboard["charts"])
    _remember_dashboard(ctx, dashboard)
    # P4: the executive summary (rank_insights) is pinned to the top of the
    # rendered surface via the dashboard's notes.
    summary = ctx.build_state.get("exec_summary")
    if isinstance(summary, dict) and summary.get("bullets"):
        dashboard["notes"] = [*summary["bullets"][:5], *(dashboard.get("notes") or [])]
    ctx.report(
        f"Profiled {dashboard['rows']} rows × {len(dashboard['columns'])} columns — "
        f"choosing the charts…"
    )
    cols = ", ".join(f"{c['name']} ({c['kind']})" for c in dashboard["columns"])
    notes = dashboard.get("notes") or []
    notes_line = f" Data notes to weave in: {'; '.join(notes)}." if notes else ""
    # GROUND TRUTH: the model may only quote figures computed HERE — qwen was
    # inventing plausible-but-wrong averages in the prose while the cards were
    # right. Hand it the exact card values and forbid any other numbers.
    stat_facts = "; ".join(
        f"{c['title']} = {c['value']}" for c in dashboard["charts"] if c.get("kind") == "stat"
    )
    facts_line = (
        f" GROUND-TRUTH NUMBERS — if you mention ANY figure, copy it EXACTLY from "
        f"this list and mention no other numbers: {stat_facts}."
        if stat_facts
        else " Do not state any numeric values in your reply."
    )
    return ToolResult(
        content=(
            f"Built a dashboard from {name} with {len(dashboard['charts'])} charts "
            f"(columns: {cols}). It's shown to the user in the chat.{notes_line}{facts_line} "
            "Introduce it like an analyst: 1-2 sentences on what the dashboard shows "
            "and how to read it, then the data notes above (they're computed from the "
            "real file — never invent or recompute numbers), then offer next moves: add "
            "or change charts (pie/line/table/stat), focus on a column, or build the "
            "same dashboard on their Grafana or Power BI."
        ),
        summary=f"{len(dashboard['charts'])} charts from {name}",
        ui={"command": "open_dashboard", "dashboard": dashboard},
    )


async def _create_powerbi_report(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Push the attached CSV into the user's own Power BI as a push dataset (My
    workspace) and hand back the link whose "Create a report" -> Auto-create
    builds the visuals. Rides the user's Power BI connection (Nango OAuth)."""
    from sila.adapters.connectors.nango_client import NangoError
    from sila.services.connectors.connector_service import ConnectorError
    from sila.services.connectors.powerbi_service import PowerBIError, PowerBIService

    files = ctx.build_state.get("uploaded_files") or []
    wanted = str(args.get("file", "")).strip().lower()
    data_files = [
        f
        for f in files
        if isinstance(f, dict) and str(f.get("name", "")).lower().endswith((".csv", ".txt"))
    ]
    if wanted:
        named = [f for f in data_files if wanted in str(f.get("name", "")).lower()]
        data_files = named or data_files
    if not data_files:
        return ToolResult(
            content=(
                "No data file is attached. Ask the user to attach a CSV file "
                "(the paperclip in the composer) and ask again."
            ),
            summary="No data file",
        )
    chosen = _pick_data_file(ctx, data_files)
    fname = str(chosen.get("name", "data.csv"))
    gate = await _template_propose_gate(ctx, chosen, deliverable="Power BI report")
    if gate is not None:
        return gate
    title = str(args.get("title", "")).strip() or fname.rsplit(".", 1)[0]
    # Continuity beats the model's title whims: once a dataset exists for this
    # session, keep refreshing THAT one under its original name.
    stored_id = str(ctx.build_state.get("powerbi_dataset_id") or "") or None
    if stored_id and ctx.build_state.get("powerbi_dataset_title"):
        title = str(ctx.build_state["powerbi_dataset_title"])

    ctx.report(f"Reading {fname}…")
    # MIRROR the in-chat/Grafana dashboard exactly: build the same chart list
    # and hand its RECIPES (kind/metric/by/columns) to the PBIX generator as
    # the authoritative visual set — no independent re-derivation (Mojahid:
    # "why different in grafana and powerbi, not same?").
    recipes: list[dict[str, Any]] | None = None
    try:
        mirror = await _spec_for_chosen(
            ctx,
            chosen,
            focus=str(args.get("focus", "")) or None,
            extra_charts=_merge_chart_edits(ctx, args),
        )
        mirror["charts"] = _prune_charts(ctx, mirror["charts"])
        _remember_dashboard(ctx, mirror)
        recipe_keys = ("kind", "title", "metric", "by", "agg", "columns", "limit", "series_cols")
        recipes = [{k: c[k] for k in recipe_keys if c.get(k) is not None} for c in mirror["charts"]]
    except ValueError:
        recipes = None  # unprofilable → let Power BI derive its own visuals
    try:
        csv_text, sampled = await _dataset_csv_sample(ctx, chosen)
        result = await PowerBIService(ctx.db).push_csv_dataset(
            user_id=ctx.user.id,
            workspace_id=ctx.workspace_id,
            filename=fname,
            csv_text=csv_text,
            title=title,
            dataset_id=stored_id,
            extra_charts=recipes if recipes else _merge_chart_edits(ctx, args),
            charts_authoritative=bool(recipes),
            progress=ctx.report,
        )
    except ValueError as exc:
        return ToolResult(content=f"Couldn't use {fname}: {exc}", summary="Unusable data")
    except PowerBIError as exc:
        log.warning("powerbi.push_failed", error=str(exc))
        return ToolResult(
            content=(
                f"Power BI failed: {exc}. TELL THE USER this plainly (don't hide it), and "
                "still deliver any OTHER outputs they asked for in the same message."
            ),
            summary="Power BI limit",
        )
    except ConnectorError:
        return ToolResult(
            content=(
                "Power BI isn't connected yet. Tell the user to open the **Connectors** "
                "page, connect Power BI (Microsoft sign-in), then ask again."
            ),
            summary="Power BI not connected",
        )
    except NangoError as exc:
        return ToolResult(
            content=(
                f"Power BI rejected the request: {exc}. Note: this API needs a "
                "work/school Microsoft account with a (free) Power BI license — "
                "personal accounts are refused."
            ),
            summary="Power BI error",
        )
    ctx.build_state["powerbi_dataset_id"] = result.dataset_id
    ctx.build_state["powerbi_dataset_title"] = title
    verb = "Updated" if result.updated else "Created"
    sample_note = (
        f" NOTE: Power BI holds the first {_PBIX_MAX_ROWS:,} rows (a sample of the full dataset) — "
        "the chat/Grafana numbers cover the WHOLE file; say so."
        if sampled
        else ""
    )
    pushed = (
        f"{verb} the push dataset '{title}' in the user's Power BI (My workspace) "
        f"with {result.rows} rows × {result.columns} columns from {fname}.{sample_note}"
    )
    if result.report_url:
        # The full chat→dashboard experience: a generated report with visuals.
        return ToolResult(
            content=(
                f"{pushed} A report with charts was ALSO auto-built — give the user "
                f"ONLY this link, as [open your Power BI dashboard]({result.report_url}). "
                "NEVER mention a dataset URL — not even if earlier messages in this "
                "conversation did (that link confuses users; the dashboard link is "
                "everything they need). STRUCTURE YOUR REPLY IN 3 PARTS (a bare link "
                "reads lazy): (1) 1-2 sentences on what landed in their Power BI and "
                "that it mirrors the chat dashboard; (2) the link; (3) two short "
                "pointers on what to explore there (the report is LIVE — they can "
                "click any bar to cross-filter) plus an offer: change charts here and "
                "re-run to refresh the snapshot."
            ),
            summary=f"Built Power BI dashboard: {title}"[:80],
        )
    if result.report_failed:
        # The reverse-engineered report step broke (likely a Power BI-side
        # change) — the agreed graceful message, data still delivered.
        return ToolResult(
            content=(
                f"{pushed} The automatic report couldn't be generated this time — "
                "we're facing an issue on this connector's report step and we're "
                "working on it. Meanwhile give the user this link as "
                f"[open it in Power BI]({result.url}) and tell them to click "
                '"Create a report" → "Auto-create" there for instant visuals.'
            ),
            summary=f"Pushed to Power BI: {title}"[:80],
        )
    return ToolResult(
        content=(
            f"{pushed} Give the user this link as [open it in Power BI]({result.url}) "
            'and tell them to click "Create a report" → "Auto-create" there for '
            "instant visuals. The data is a snapshot of the file; re-run to refresh it."
        ),
        summary=f"Pushed to Power BI: {title}"[:80],
    )


def _deck_from_outline(
    title: str, palette: str, outline: list[dict[str, Any]], cover_image: str = ""
) -> dict[str, Any]:
    """Legacy {title, bullets} outline → a Slides-Engine deck dict. The first
    slide is a hero cover; the rest become card grids (one card per bullet).
    Renders through the same engine as a rich deck, so a simple outline still
    gets the palette, icons-capable layouts, and decorative styling. When
    `cover_image` is set, the cover becomes an image-feature slide with a
    generated hero picture — so the model gets a cover image WITHOUT having to
    hand-build the full rich deck (which it does unreliably, collapsing the
    other slides)."""
    slides: list[dict[str, Any]] = []
    for i, raw in enumerate(outline):
        heading = str(raw.get("title", "")).strip()
        bullets = [str(b).strip() for b in (raw.get("bullets") or []) if str(b).strip()]
        if i == 0:
            cover: dict[str, Any] = {
                "slideTitle": heading or title,
                "subtitle": " · ".join(bullets[:2]),
            }
            if cover_image.strip():
                cover["layoutType"] = "image-feature"
                cover["containers"] = [{"type": "image", "imagePrompt": cover_image.strip()}]
            else:
                cover["layoutType"] = "title-hero"
            slides.append(cover)
        elif bullets:
            slides.append(
                {
                    "layoutType": "3-column-cards",
                    "slideTitle": heading,
                    "containers": [{"type": "card", "body": b} for b in bullets],
                }
            )
        else:
            slides.append({"layoutType": "title-hero", "slideTitle": heading or title})
    return {"title": title, "palette": palette, "slides": slides}


async def _create_slide_deck(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Build a .pptx with the deterministic Slides Engine and save it to the
    user's OneDrive (else a download link). The model passes a simple
    {title, bullets} outline OR a rich `deck` (layouts/charts/tables/icons);
    a named workspace template contributes its brand colors + fonts."""
    from sila.db.repositories.pptx_templates import PptxTemplateRepository
    from sila.services.connectors.connector_service import ConnectorError, ConnectorService
    from sila.services.slides import render as slide_render
    from sila.services.slides import schema as slide_schema
    from sila.services.slides.brand_extract import BrandExtractionError, extract_brand_tokens
    from sila.services.slides.template_fill import stencil_book_for

    title = str(args.get("title", "")).strip() or "Presentation"
    palette = str(args.get("palette", "") or "modern-slate").strip()

    # A rich deck (with layouts/charts/tables) takes precedence; otherwise
    # convert the simple bullet outline. Either way it goes through the gate.
    deck_arg = args.get("deck")
    if isinstance(deck_arg, dict) and isinstance(deck_arg.get("slides"), list):
        deck_dict = {
            "title": str(deck_arg.get("title") or title),
            "palette": str(deck_arg.get("palette") or palette),
            "slides": deck_arg["slides"],
        }
    else:
        outline = args.get("slides") if isinstance(args.get("slides"), list) else []
        if not outline:
            return ToolResult(
                content=(
                    "No slides provided. Pass either `slides`: "
                    '[{"title": str, "bullets": [str,…]}, …] or a rich `deck` '
                    "(layouts/charts/tables) built from the user's content."
                )
            )
        deck_dict = _deck_from_outline(
            title, palette, outline, cover_image=str(args.get("cover_image", "")).strip()
        )

    try:
        deck, _changelog = slide_schema.validate_deck(deck_dict)
    except slide_schema.DeckValidationError as exc:
        return ToolResult(
            content=(
                "The deck couldn't be built: "
                + "; ".join(exc.violations)
                + ". Fix these and call create_slide_deck again."
            ),
            summary="Deck needs fixes",
        )

    # A named workspace/built-in template contributes brand tokens (colors +
    # fonts) that override the chosen palette; a bad template silently falls
    # back to the palette rather than failing the deck.
    repo = PptxTemplateRepository(ctx.db)
    wanted = str(args.get("template", "")).strip()
    tokens = None
    designed = None
    template_bytes: bytes | None = None
    template_name = f"{deck.palette.value} palette"
    from sila.services.connectors.builtin_pptx import THEMES, builtin_by_name

    if wanted and wanted.lower() not in ("default", "sila default", "none"):
        tpl = await repo.find_by_name(ctx.workspace_id, wanted)
        if tpl is not None:
            template_bytes, template_name = tpl.file_bytes, tpl.name
        else:
            hit = builtin_by_name(wanted)
            if hit is not None:
                template_name, template_bytes = hit
            else:
                names = [t.name for t in await repo.list_for_workspace(ctx.workspace_id)]
                listing = ", ".join(names + [t.name for t in THEMES]) or "none"
                return ToolResult(
                    content=(
                        f"No template named '{wanted}'. Available: {listing}. "
                        "Or omit `template` to use a built-in palette."
                    ),
                    summary="Template not found",
                )
        if template_bytes:
            # A DESIGNED template (branding drawn on its slides, not carried by
            # its master/theme) is rendered by cloning those slides. Extracting a
            # palette from such a file yields the STOCK Office theme, which is
            # how a deck came back teal instead of Omantel blue.
            designed = stencil_book_for(template_bytes)
            if designed is None:
                try:
                    tokens = extract_brand_tokens(template_bytes)
                except BrandExtractionError:
                    tokens = None  # unreadable theme → keep the palette

    # Generate any requested images (image-feature slides). Opt-in, GPU-bound,
    # and gracefully degrading: with no image-gen container the containers keep
    # image_bytes=None and the renderer draws a styled placeholder.
    image_containers = [
        c for s in deck.slides for c in s.containers if c.is_image and c.image_prompt.strip()
    ]
    if image_containers and settings.image_gen_url:
        from sila.adapters.image.flux import FluxImageAdapter

        ctx.report(f"Generating {len(image_containers)} image(s)… (this takes a moment)")
        gateway = FluxImageAdapter(
            settings.image_gen_url,
            settings.ollama_url,
            settings.sila_llm_model,
            gen_timeout_s=settings.image_gen_timeout_s,
        )
        images = await gateway.generate_images([c.image_prompt for c in image_containers])
        for container, png in zip(image_containers, images, strict=True):
            container.image_bytes = png

    ctx.report(f"Designing {len(deck.slides)} slides ({template_name})…")
    if designed is not None and template_bytes is not None:
        from datetime import UTC, datetime

        # deck_guard defines its OWN DeckValidationError (a RuntimeError), which
        # is NOT the schema's (a ValueError). Aliased so the two never blur.
        from sila.services.slides.deck_guard import DeckValidationError as GuardRejection
        from sila.services.slides.template_fill import fill_template
        from sila.services.slides.template_map import deck_to_slides

        # The build-time guard REJECTS a damaged or unreadable deck by raising.
        # Uncaught, that became a bare "Failed" with the reason only in the log,
        # so the model retried blind and stripped content until something fit.
        # Handing back the violations makes the next attempt a targeted fix.
        try:
            deck_bytes = fill_template(
                template_bytes,
                deck_to_slides(deck, stencils=designed),
                deck_name=title,
                year=str(datetime.now(UTC).year),
                stencils=designed,
            )
        except GuardRejection as exc:
            return ToolResult(
                content=(
                    f"The branded deck was rejected before delivery: {exc}. "
                    "These are per-slide. 'content text at Npt' means that slide "
                    "holds too much to stay readable — cut or split its longest "
                    "bodies rather than rewording them. Then call "
                    "create_slide_deck again with the same template."
                ),
                summary="Deck needs fixes",
            )
    else:
        deck_bytes = slide_render.render_deck(deck, tokens=tokens)
    slides = deck.slides  # for the count in the delivery messages

    safe = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "-").lower() or "deck"
    filename = f"{safe}.pptx"
    ctx.report("Saving the deck to your OneDrive…")
    try:
        resp = await ConnectorService(ctx.db).execute_proxy(
            user_id=ctx.user.id,
            workspace_id=ctx.workspace_id,
            provider="ms-powerpoint",
            method="PUT",
            endpoint="powerpoint/upload",
            data={"title": title, "filename": filename, "__deck_bytes__": deck_bytes},
        )
    except ConnectorError:
        # No OneDrive connection — the deck must still reach the user: store it
        # and hand back a 24h download link (capability URL).
        from sila.db.repositories.generated_files import GeneratedFileRepository

        row = await GeneratedFileRepository(ctx.db).create(
            workspace_id=ctx.workspace_id,
            user_id=ctx.user.id,
            filename=filename,
            content_type=(
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
            file_bytes=deck_bytes,
        )
        # The capability URL must be LIVE before the user clicks it — the WS
        # session commits lazily, and an uncommitted row 404s (live bug: pptx).
        await ctx.db.commit()
        dl = f"{settings.sila_api_public_url.rstrip('/')}/v1/files/{row.id}"
        # The gateway runs on the SOURCE slides, not the rendered file: every
        # string that reached OOXML passed through `deck` first, and a slide
        # number is where the user will look, an XML part is not.
        from sila.services.sila.deliverable_guard import (
            deck_chunks,
            describe_for_model,
            guard_chunks,
        )

        security = await guard_chunks(deck_chunks(deck))
        gate_note = describe_for_model(security, artifact=title)
        return ToolResult(
            content=(
                f"Built '{title}' ({len(slides)} slides, '{template_name}' template). "
                "The deck is ATTACHED to your reply as a download card — do NOT "
                "paste a download link or a URL in your text; the card is the "
                "download and repeating it just clutters the message. Say the deck "
                "is ready, describe what is in it, and mention they can connect "
                "PowerPoint on the Connectors page to save future decks to OneDrive "
                "automatically (the link is valid 24h)." + (f"\n\n{gate_note}" if gate_note else "")
            ),
            summary=(
                f"Blocked by the security gateway: {title}"
                if security["status"] == "blocked"
                else f"Deck ready: {title}"
            )[:80],
            meta={
                "artifacts": [
                    _artifact(
                        "deck",
                        title,
                        url=dl,
                        subtitle=f"PowerPoint · {len(slides)} slides",
                        security=security,
                    )
                ]
            },
        )
    web_url = str(resp.get("webUrl", "")) if isinstance(resp, dict) else ""
    link = (
        " It is ATTACHED to your reply as a card — do NOT paste the URL in your text."
        if web_url
        else ""
    )
    return ToolResult(
        content=(
            f"Built '{title}' ({len(slides)} slides, '{template_name}' template) and "
            f"saved {filename} to OneDrive.{link}"
        ),
        summary=f"Deck: {title} · {len(slides)} slides"[:80],
        meta={
            "artifacts": [
                _artifact(
                    "deck",
                    title,
                    url=web_url or None,
                    subtitle=f"PowerPoint · {len(slides)} slides · OneDrive",
                )
            ]
        },
    )


async def _search_bazaar(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query", "")).strip()
    # An empty query is a browse-all: search_catalog returns the entire published
    # catalogue, which this tool then labels "Top matches". That is how a request
    # for an investment agent came back recommending "Daily Education Summary" —
    # the only published agent, matched against nothing. Refuse instead.
    if len(query) < 3:
        return ToolResult(
            content=(
                "No search was run — the query was empty. Search again using the "
                "user's own words for what the agent should DO. Never present "
                "catalogue entries as matches for a search you did not run."
            ),
            summary="No search terms",
        )
    # user_id drives the provenance split (own / org / third-party) and is
    # REQUIRED — omitting it raised TypeError on every call, which surfaced to
    # the user as "an unexpected error with the search_bazaar function".
    result = await _bazaar_service(ctx.db).search_catalog(
        query,
        category_ids=[],
        page=1,
        size=5,
        calling_workspace_id=ctx.workspace_id,
        user_id=ctx.user.id,
    )
    items = result.get("items", [])
    if not isinstance(items, list) or not items:
        # Same anti-loop wording as web_search: without it the model re-ran the
        # identical search on the next round and the user watched "Searching the
        # Bazaar / No matches found" twice before anything happened.
        return ToolResult(
            content=(
                "No published agents matched. The Bazaar has nothing for this — do "
                "NOT search again with different wording. Tell the user there is no "
                "ready-made agent and continue with what they asked for."
            ),
            summary="No matches found",
        )
    lines = [
        f"- {a['name']} (id={a['id']}, kind={a.get('kind', 'chat')}, "
        f"hired={'yes' if a.get('hired') else 'no'}): {str(a.get('description', ''))[:120]}"
        for a in items
    ]
    plural = "s" if len(items) != 1 else ""
    return ToolResult(
        content="Top matches:\n" + "\n".join(lines),
        summary=f"Found {len(items)} agent{plural}",
    )


async def _hire_agent(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raw_id = str(args.get("agent_id", "")).strip()
    try:
        agent_id = uuid.UUID(raw_id)
    except ValueError:
        # Reached when the model invents an id, which in practice means it chose
        # `hire` for a request to BUILD. Phrased as an instruction to recover, and
        # explicitly not-for-the-user, because the model used to relay the old
        # wording to the user as if it were the answer. The summary stops the UI
        # resting on its optimistic "Hiring that agent…" label.
        return ToolResult(
            content=(
                "NOT hired — no such agent. Do not repeat this message to the user. "
                "You supplied an agent_id that did not come from search_bazaar. If "
                "they asked you to BUILD a new agent, start the agent-building flow "
                "instead. If they want an existing one, call search_bazaar and show "
                "them the matches so they can choose."
            ),
            summary="Couldn't hire — no agent chosen",
        )
    background = BackgroundTasks()
    try:
        await _bazaar_service(ctx.db).hire(
            workspace_id=ctx.workspace_id,
            agent_id=agent_id,
            hired_by=ctx.user.id,
            background_tasks=background,
        )
    except HTTPException as exc:
        return ToolResult(content=f"Couldn't hire: {exc.detail}", summary="Couldn't hire")
    await background()  # run the audit task the service queued
    return ToolResult(
        content="Hired. The agent is now in the workspace's hired list.",
        summary="Hired — added to your workspace",
        ui={"command": "navigate", "path": "/bazaar/hired"},
    )


async def _request_documents(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    prompt = str(args.get("prompt") or "Upload the documents I should use.")
    return ToolResult(
        content=(
            "Opened an upload panel for the user. They will upload files; "
            "their next message will tell you what arrived."
        ),
        summary="Asked for documents",
        ui={"command": "open_modal", "modal": "upload", "prompt": prompt},
    )


# NOTE: the legacy `create_agent_from_docs` (instant Dify chat-app build) was REMOVED
# from the registry: it predated the requirements gate, so a model picking it skipped
# the whole interview + confirm ("Created draft agent" with zero questions). Every
# build now goes through create_agent_workflow → the deterministic gate.


def _normalize_questions(raw: Any) -> list[dict[str, Any]]:
    """Validate the model's questions into the ask_user contract. Caps at 3 (it
    should never feel like a form) and drops malformed entries."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for i, q in enumerate(raw[:3]):
        if not isinstance(q, dict):
            continue
        question = str(q.get("question") or "").strip()
        opts_raw = q.get("options")
        options = (
            [str(o).strip() for o in opts_raw if str(o).strip()][:8]
            if isinstance(opts_raw, list)
            else []
        )
        if not question or not options:
            continue
        select = "multi_select" if q.get("select") == "multi_select" else "single_select"
        out.append(
            {
                "id": str(q.get("id") or f"q{i + 1}"),
                "question": question[:200],
                "select": select,
                "options": options,
                "allow_other": bool(q.get("allow_other")),
            }
        )
    return out


async def _ask_user(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Ask the user one or more choice questions, rendered as a bottom-sheet wizard.

    The model decides what to ask and the options; we just relay the structured
    payload. (This is the same shape a LangGraph interrupt() will carry once the
    orchestrator lands — the bottom-sheet selection becomes the resume value.)
    """
    questions = _normalize_questions(args.get("questions"))
    if not questions:
        return ToolResult(
            content="No valid questions — ask the user directly in plain text instead."
        )
    request_id = str(uuid.uuid4())
    return ToolResult(
        content=(
            f"Presented {len(questions)} question(s) to the user as a chooser. Their selected "
            "answers arrive in the next message — wait for them, then continue."
        ),
        summary="Asked the user",
        ui={"command": "ask_user", "request_id": request_id, "questions": questions},
    )


async def _pdf_sibling(
    ctx: ToolContext,
    data: bytes,
    filename: str,
    title: str,
    *,
    wanted: bool,
    security: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """A PDF rendering of a document just produced, as an extra artifact.

    Asking for "a PDF and a Word doc" used to need two prompts because there was
    no PDF path at all — the registry could only make Office files. Both formats
    now come back from one call.

    Best-effort: the .docx is already built and attached, so a Gotenberg outage
    costs the PDF and never the document.
    """
    if not wanted:
        return []
    from sila.db.repositories.generated_files import GeneratedFileRepository
    from sila.services.office_convert import office_to_pdf

    pdf = await office_to_pdf(data, filename)
    if not pdf:
        return []
    row = await GeneratedFileRepository(ctx.db).create(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user.id,
        filename=f"{filename.rsplit('.', 1)[0]}.pdf",
        content_type="application/pdf",
        file_bytes=pdf,
    )
    # Same rule as the docx: the capability URL must be live before the click.
    await ctx.db.commit()
    url = f"{settings.sila_api_public_url.rstrip('/')}/v1/files/{row.id}"
    # Same content, same verdict: a blocked .docx must not ship as an open .pdf.
    extra = {"security": security} if security else {}
    return [_artifact("pdf", f"{title} (PDF)", url=url, subtitle="PDF", **extra)]


def _artifact(
    kind: str,
    name: str,
    *,
    url: str | None = None,
    subtitle: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One shape for anything a turn HANDS BACK — a deck, a document, a sheet, a
    dashboard, an agent.

    These used to live only in prose ("give the user this DOWNLOAD link… as
    [download the deck](url)"), which made whether the user ever saw them depend
    on the model repeating a markdown link correctly. Carried as data instead,
    the UI renders an attachment tile per artifact whether the model mentions it
    or not — the same fix the search citations and the built agent already got.

    `kind` drives the icon and the type label, so it must stay one of the values
    the frontend knows: agent · deck · doc · sheet · pdf · dashboard.
    """
    return {"kind": kind, "name": name, "url": url, "subtitle": subtitle, **extra}


def _plan_recap(plan: dict[str, Any]) -> str:
    """A compact text form of the plan for the model's own transcript.

    Only the fields that define the build. Steps are capped — the recap exists so
    the model can act on its own proposal later, not to reprint the whole card.
    O(len(plan)).
    """
    lines: list[str] = []
    for label, key in (
        ("Goal", "goal"),
        ("Content", "content"),
        ("Sources", "sources"),
        ("Schedule", "schedule"),
        ("Destination", "destination"),
        ("When nothing is new", "fallback"),
    ):
        value = plan.get(key)
        if value:
            lines.append(f"- {label}: {value}")
    integrations = plan.get("integrations") or []
    if integrations:
        lines.append(f"- Integrations: {', '.join(str(i) for i in integrations)}")
    steps = plan.get("steps") or []
    if steps:
        rendered = "; ".join(str(st.get("title") or st) for st in steps[:6])
        lines.append(f"- Steps: {rendered}")
    return "\n".join(lines) or "- (no details captured)"


async def _propose_plan(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Render the Plan-mode plan card and pause for Accept / Adjust / Discard.

    The card (persisted in the trace, like the preview block) and the pausing
    decision share one request_id, so the card itself carries the buttons and
    the docked sheet stays suppressed. Accept is handled CLIENT-side: it seeds
    the Build interview and switches mode — nothing is built here, ever.
    """
    if ctx.build_state.get("active_template"):
        return ToolResult(
            content=(
                "A template session is pinned — its own staged flow handles "
                "planning and confirmation. Continue that flow instead of "
                "proposing a generic agent plan."
            ),
            summary="Template session — plan card not applicable",
        )
    from sila.services.sila.plan_artifact import build_plan

    plan, seed = build_plan(args)
    if not plan:
        return ToolResult(
            content=(
                "No goal to plan around yet — ask the user what they want the agent "
                "to achieve (one sentence), then propose the plan."
            )
        )
    request_id = str(uuid.uuid4())
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(
            {
                "type": "ui",
                "command": "open_plan",
                "request_id": request_id,
                "plan": plan,
                # Slot-level mapping is expert detail — the tier serializer strips
                # this key for Beginner sockets (the friendly card stays).
                "expert": {"slots": dict(seed.get("filled", {}))},
            }
        )
    except Exception:  # noqa: BLE001 — no stream writer (loop path/tests) → sheet fallback
        log.info("sila.plan.card_emit_skipped")
    # The plan itself goes into the tool CONTENT, not just the card. Without it
    # the model's transcript read "Proposed the plan to the user as a card" and
    # nothing else — so on the next turn it answered "I don't have a plan on
    # record in this chat" about a plan it had just written. The card is for the
    # user; this recap is the model's own memory of what it proposed.
    recap = _plan_recap(plan)
    return ToolResult(
        content=(
            "Proposed this plan to the user as a card:\n\n"
            f"{recap}\n\n"
            "They can Accept (the build starts, pre-filled from this plan), Adjust "
            "(their feedback arrives next — update the plan and propose again), or "
            "Discard. Wait for their decision. If they approve in their own words "
            "rather than clicking, treat THIS plan as the agreed spec and build it — "
            "do not ask them to specify it again."
        ),
        summary="Proposed a plan",
        ui={
            "command": "ask_user",
            "request_id": request_id,
            "questions": [
                {
                    "id": "__plan__",
                    "question": "Ready to go with this plan?",
                    "select": "single_select",
                    "options": ["Accept & build", "Adjust the plan", "Discard"],
                    "allow_other": True,
                }
            ],
        },
    )


async def _request_credentials(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Open the masked 'Incognito' panel to collect a secret for an integration.

    The value is created directly in the integration's ENCRYPTED store (the user's own
    n8n credential store, or an MCP server record) and never enters the chat, the model,
    or the logs. Only curated providers are allowed; MCP needs an admin.
    """
    from sila.services.sila.credential_service import fields_payload, spec_for

    provider = str(args.get("provider", "")).strip().lower()

    # Email is sent through the platform's OWN mail server (sovereign SMTP): the recipient
    # is baked into a capability token and Sila sends it — n8n holds no creds and the user
    # has NOTHING to connect. Never open an SMTP panel for it; tell the model it's wired.
    if provider == "email":
        return ToolResult(
            content=(
                "Email is sent through the platform's built-in mail server — there's nothing "
                "to connect and no password to enter. It's already wired; just continue the "
                "build (capture the recipient address if you don't have it yet)."
            ),
            summary="Email ready (platform mail)",
        )

    # OAuth providers (Slack, Google) use sign-in via Nango, NOT a pasted token. Even
    # if the model calls this (wrong) tool, route it to the Connect flow so the user
    # never sees a token box for an OAuth app. Falls through to the B2 panel only when
    # Nango isn't provisioned (no secret key).
    from sila.services.connectors.manifest import spec_for as connector_spec_for

    cspec = connector_spec_for(provider)
    if cspec is not None and cspec.auth_type == "oauth" and settings.nango_secret_key:
        return ToolResult(
            content=(
                f"{cspec.label} uses sign-in (OAuth), not a pasted token. Opening the "
                "Connect panel so the user authorizes it and picks the destination — wait "
                "for their confirmation, then continue the build."
            ),
            summary=f"Connect {cspec.label}",
            ui={
                "command": "connect_provider",
                "request_id": str(uuid.uuid4()),
                "provider": cspec.provider,
                "label": cspec.label,
            },
        )

    spec = spec_for(provider)
    if spec is None:
        return ToolResult(
            content=(
                f"'{provider}' isn't a connectable provider. Supported: slack, email, mcp. "
                "Pick one of those, or build without that integration."
            )
        )
    if spec.admin_only and ROLE_RANK[ctx.role] < ROLE_RANK[Role.ADMIN]:
        return ToolResult(
            content=f"Connecting {spec.label} needs an admin. Ask an admin, or skip it."
        )
    request_id = str(uuid.uuid4())
    return ToolResult(
        content=(
            f"Opened a secure, masked panel for the user to enter their {spec.label} "
            "credential. It is NOT saved in this chat. Their confirmation arrives in the "
            "next message — wait for it, then continue the build."
        ),
        summary=f"Asked for {spec.label} credentials",
        ui={
            "command": "request_credentials",
            "request_id": request_id,
            "provider": spec.provider,
            "label": spec.label,
            "fields": fields_payload(spec),
        },
    )


async def _resolve_oauth_connectors(
    ctx: ToolContext, integrations: list[str], filled: dict[str, Any]
) -> tuple[dict[str, Any], str | None, ToolResult | None]:
    """For each OAuth-manifest integration, resolve the user's Nango binding into
    a proxy ConnectorNode. If one isn't connected yet, return a `connect_provider`
    interrupt so the build pauses and opens the Connect flow (Slice 6).

    `filled` is the build interview's answers: connectors without a resource
    picker (Google least-privilege) declare a `slot_arg` in the manifest, and the
    captured slot value fills `slot_field` in the node data (what the payload
    builder reads — gmail "to", sheets "spreadsheet_id").

    Returns (connector_nodes, capability_token, interrupt). When `interrupt` is
    set, the caller must return it immediately.
    """
    from sila.db.repositories.connectors import ConnectorBindingRepository
    from sila.services.connectors import capability
    from sila.services.connectors.manifest import spec_for as connector_spec_for
    from sila.services.n8n_workflow_builder import ConnectorNode

    nodes: dict[str, Any] = {}
    # If the Nango connector stack isn't provisioned (no secret key), don't route
    # OAuth providers through it — let them fall to the B2 typed-credential modal.
    if not settings.nango_secret_key:
        return {}, None, None
    bindings = ConnectorBindingRepository(ctx.db)
    for integ in integrations:
        cspec = connector_spec_for(integ)
        if cspec is None or cspec.auth_type != "oauth":
            continue  # not an OAuth connector — handled by the B2 path
        if cspec.action is None:
            continue  # connectable + pickable, but no agent action wired (manifest)
        try:
            binding = await bindings.get(
                user_id=ctx.user.id, workspace_id=ctx.workspace_id, provider=cspec.provider
            )
        except Exception as exc:  # noqa: BLE001 — connectors not migrated → fall to B2
            log.warning("sila.connector_unavailable", provider=integ, error=type(exc).__name__)
            continue
        if binding is None:
            return (
                {},
                None,
                ToolResult(
                    content=(
                        f"This agent needs {cspec.label}. Opening the secure connect "
                        f"panel — authorize {cspec.label} (no token to paste), pick the "
                        "destination, and I'll finish building."
                    ),
                    summary=f"Connect {cspec.label}",
                    ui={
                        "command": "connect_provider",
                        "request_id": str(uuid.uuid4()),
                        "provider": cspec.provider,
                        "label": cspec.label,
                    },
                ),
            )
        # Build the action payload generically from the manifest (no provider
        # branch): static fields + the chosen resource → its payload field.
        action = cspec.action
        data: dict[str, Any] = dict(action.static)
        if action.resource_arg and action.resource_field:
            chosen = (binding.selected_resources or {}).get(action.resource_arg)
            if isinstance(chosen, dict) and chosen.get("id"):
                data[action.resource_field] = chosen["id"]
        # No resource picker → the target came from the build interview instead
        # (least-privilege Google connectors: gmail_to → "to", sheet_id →
        # "spreadsheet_id"). Normalized (a pasted Sheets URL → the bare id).
        if action.slot_arg and action.slot_field:
            from sila.services.sila.build_slots import normalize_destination

            captured = str(filled.get(action.slot_arg) or "").strip()
            if captured:
                data[action.slot_field] = normalize_destination(action.slot_arg, captured)
        # Capability bound to (provider + this exact endpoint + this connection) —
        # not a provider-wide bearer. It can ONLY trigger this one action on this
        # one connection, and dies when the binding is revoked.
        token = capability.mint(
            workspace_id=ctx.workspace_id,
            user_id=ctx.user.id,
            provider=cspec.provider,
            endpoint=action.endpoint,
            connection_id=binding.nango_connection_id,
        )
        nodes[integ] = ConnectorNode(
            provider=cspec.provider,
            endpoint=action.endpoint,
            method=action.method,
            data=data,
            token=token,
            # The generated summary flows into this field at runtime ($json.summary).
            message_field=action.message_field,
        )

    # Per-node tokens are authoritative; the spec-level token stays None.
    return nodes, None, None


_CONFIRM_SLOT = "__confirm__"


def _slot_ask(
    slot_name: str, question: str, options: list[str], lead: str, request_id: str | None = None
) -> ToolResult:
    """A pausing ask_user emitted BY the build gate (deterministic, not the model).
    `slot` tags which requirement this fills so the WS handler records the answer. An
    explicit `request_id` lets the confirm reuse the preview's id (accept-in-preview)."""
    return ToolResult(
        content=lead,
        summary="Gathering requirements",
        ui={
            "command": "ask_user",
            "request_id": request_id or str(uuid.uuid4()),
            "slot": slot_name,
            # Field names MUST match the frontend SilaAskQuestion (question/select/options),
            # else the sheet renders a blank question — the whole gate looked question-less.
            "questions": [
                {
                    "id": slot_name,
                    "question": question,
                    "select": "single_select",
                    "options": options,
                    "allow_other": True,
                }
            ],
        },
    )


def _guess_changed_slot(text: str, agent_type: str) -> str | None:
    """Map a free-text change request ('make it weekdays only at 5pm') to the slot
    it most plausibly edits — deterministic keywords, no model. None = unguessable."""
    from sila.services.sila import build_slots as bs

    names = {s.name for s in bs.REQUIRED_SLOTS.get(agent_type, ())}
    t = text.lower()
    if (
        "schedule" in t
        or re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", t)
        or any(w in t for w in ("weekday", "weekly", "daily", "every day", "morning", "evening"))
    ) and "schedule" in names:
        return "schedule"
    if ("#" in t or "channel" in t) and "destination" in names:
        return "destination"
    if ("sheet" in t or "spreadsheet" in t) and "sheet_id" in names:
        return "sheet_id"
    if ("space" in t or "chat" in t) and "chat_space" in names:
        return "chat_space"
    if "gmail" in t and "gmail_to" in names:
        return "gmail_to"
    if "outlook" in t and "outlook_to" in names:
        return "outlook_to"
    if ("@" in t or "email" in t) and "email_to" in names:
        return "email_to"
    if ("name" in t or "call it" in t) and "name" in names:
        return "name"
    if (
        any(w in t for w in ("content", "post", "section", "summar", "output"))
        and "content" in names
    ):
        return "content"
    return None


def _valid_phrases(slots: list[Any], raw: Any) -> dict[str, str]:
    """Filter an LLM's rephrased-question payload down to safe overrides: known slot
    ids only, plain strings, sane length, actually a question. Anything else falls
    back to the slot's canonical wording. Pure — unit-testable without a model."""
    if not isinstance(raw, dict):
        return {}
    by_name = {s.name: s for s in slots}
    out: dict[str, str] = {}
    for key, val in raw.items():
        if key not in by_name or not isinstance(val, str):
            continue
        text = val.strip()
        if 10 <= len(text) <= 200 and text.endswith("?"):
            out[key] = text
    return out


async def _phrase_questions(purpose: str, slots: list[Any]) -> dict[str, str]:
    """LLM-personalised wording for the interview sheet — the same slots, phrased for
    THIS goal, so the questions don't read identical on every build. Meaning, slot ids
    and options are untouched (answer parsing depends on them); destination slots keep
    their canonical text (it carries paste-the-link instructions); any failure or
    invalid output falls back to the canonical question. One bounded model call."""
    from sila.adapters.llm.ollama import OllamaLLMAdapter

    rephrasable = [s for s in slots if not s.destination_for]
    if not rephrasable or not purpose.strip():
        return {}
    listing = "\n".join(f'- {s.name}: "{s.question}"' for s in rephrasable)
    try:
        gateway = OllamaLLMAdapter(base_url=settings.ollama_url, model=settings.sila_llm_model)
        raw = await gateway.complete_json(
            system_prompt=(
                "You rewrite interview questions for an agent-building wizard so they "
                "read naturally for the user's specific goal. Keep the EXACT meaning "
                "of each question; keep it short (max 20 words); one question mark "
                "each; write in the same language as the goal. Return ONLY JSON "
                "mapping each id to its rewritten question."
            ),
            user_prompt=f"The user's goal: {purpose[:300]}\n\nQuestions:\n{listing}",
            timeout_s=15.0,
        )
    except Exception as exc:  # noqa: BLE001 — cosmetic step; canonical wording is fine
        log.warning("sila.build.phrase_questions_failed", error=type(exc).__name__)
        return {}
    return _valid_phrases(rephrasable, raw)


def _slots_ask(slots: list[Any], lead: str, phrased: dict[str, str] | None = None) -> ToolResult:
    """One pausing ask_user carrying ALL missing requirement questions — the user answers
    everything in a single sheet (multi-step wizard), not one popup per slot. The WS
    records each answer by its question id (= slot name) on resume."""
    phrased = phrased or {}
    return ToolResult(
        content=lead,
        summary="Gathering requirements",
        ui={
            "command": "ask_user",
            "request_id": str(uuid.uuid4()),
            "questions": [
                {
                    "id": s.name,
                    "question": phrased.get(s.name, s.question),
                    "select": "single_select",
                    # "Decide for me" delegates the choice (the gate resolves it to a
                    # real value) — except destinations, which can never be guessed.
                    "options": [*s.options, *([] if s.destination_for else [_DECIDE_OPTION])],
                    "allow_other": True,
                    # The schedule slot renders a day + time-wheel picker (like the edit
                    # modal) instead of radios; it sends a cron string as the answer.
                    **({"kind": "schedule"} if s.name == "schedule" else {}),
                }
                for s in slots
            ],
        },
    )


def _picker_captured(integrations: list[str]) -> set[str]:
    """OAuth providers whose destination the Connect RESOURCE PICKER captures
    (action.resource_arg — Slack's channel). Their destination slot is skipped so
    the user isn't asked twice. Picker-less connectors (gmail, google-sheets —
    least-privilege, no list scope) are NOT in the set: their target must come
    from the interview slot. O(integrations)."""
    from sila.services.connectors.manifest import spec_for as connector_spec_for

    return {
        integ.lower()
        for integ in integrations
        if (cs := connector_spec_for(integ))
        and cs.auth_type == "oauth"
        and settings.nango_secret_key
        and cs.action is not None
        and cs.action.resource_arg is not None
    }


async def _requirements_gate(
    ctx: ToolContext,
    name: str,
    purpose: str,
    integrations: list[str],
    kb_id: str | None,
    model: str,
) -> ToolResult | None:
    """The deterministic interview gate (Part B). Returns a pausing ask_user for the
    next missing requirement, or None when the spec is complete + confirmed (build).
    At the final confirm it shows a real DRY-RUN sample of the output first."""
    from sila.services.sila import build_slots as bs

    state = ctx.build_state
    # 1. Classify the agent type once (the required-slot list depends on it).
    if not state.get("agent_type"):
        state["agent_type"] = bs.classify_agent_type(purpose, integrations)
    agent_type: str = state["agent_type"]
    filled: dict[str, Any] = state.setdefault("filled", {})
    # Delegated answers ("Decide for me", "idk", …) resolve to a real derived value
    # (a delegated destination is re-asked) — never parroted into the spec.
    bs.resolve_undecided(agent_type, filled, purpose)

    if not ctx.interactive:
        # FAIL-CLOSED (invariant): an autonomous fan-out worker cannot pause, so it
        # can never run the interview or obtain the user's confirm — it may build
        # ONLY from a spec that is already complete AND user-confirmed (e.g. seeded
        # by an accepted plan). Anything less is refused, never built on guesses.
        if state.get("confirmed") and not bs.missing_slots(agent_type, filled, integrations):
            return None
        return ToolResult(
            content=(
                "Build blocked: this task has no completed, user-confirmed spec, and an "
                "autonomous step cannot run the requirements interview. Ask the user to "
                "run this build in the chat so the interview and confirmation happen."
            ),
            summary="Build blocked — needs the interview",
        )

    # "Let me change something" — DETERMINISTIC (routing it to the model looped or
    # dropped the change on weak models). First a menu of what's changeable; picking
    # one re-asks that slot through its proper widget (the schedule wheel, options);
    # a typed correction maps by keywords and becomes the new value directly.
    if state.pop("needs_change", False):
        # FREEFORM — the question renders as plain chat and the user's next typed
        # message is the answer (no option sheet; a menu of slots read as clutter).
        # The typed correction is applied deterministically by the change mapper.
        return ToolResult(
            content="Asked the user (in chat) what they'd like to change — wait for their reply.",
            summary="What should change?",
            ui={
                "command": "ask_user",
                "request_id": str(uuid.uuid4()),
                "freeform": True,
                "questions": [
                    {
                        "id": "__change__",
                        "question": (
                            "What should I change? Tell me in your own words — "
                            "the schedule, the content, the recipient, the name…"
                        ),
                        "select": "single_select",
                        "options": [],
                        "allow_other": True,
                    }
                ],
            },
        )
    change = str(state.pop("change_slot", "") or "").strip()
    if change:
        # The spec is changing → the old confirm + preview are stale either way.
        for stale in ("confirm_rid", "preview_tried", "preview_shown"):
            state.pop(stale, None)
        state["confirmed"] = False
        slots_by_name = {s.name: s for s in bs.REQUIRED_SLOTS.get(agent_type, ())}
        picked = change.lower().strip().replace(" ", "_")
        if picked in slots_by_name:
            slot = slots_by_name[picked]
            filled.pop(picked, None)
            if not slot.must_ask:
                # Defaultable slots aren't re-asked by the missing-slots walk — ask
                # this one explicitly, then the flow re-confirms.
                return _slot_ask(
                    slot.name,
                    slot.question,
                    [*slot.options, _DECIDE_OPTION],
                    lead="One change coming up —",
                )
            # must_ask → the normal slot walk below re-asks it (with its widget).
        else:
            guessed = _guess_changed_slot(change, agent_type)
            if guessed:
                # Extract the VALUE from the sentence ("the name should be: X" → "X");
                # storing the raw sentence once made it the literal agent name.
                filled[guessed] = bs.extract_correction(guessed, change)[:300]
            # Unguessable → fall through; the refreshed confirm re-reflects the spec
            # and the user can decline again with a more specific change.

    # 1b. Ground it in documents — DETERMINISTIC (the model used to be trusted to call
    #     request_documents first; after an accepted plan there is no model round, so
    #     the gate itself asks once). Knowledge/publisher agents draw on the user's
    #     docs; the panel is skippable ("I didn't upload anything" proceeds ungrounded).
    #     A publisher sourced from the WEB is exempt: it has no use for the
    #     user's files, and asking read as the assistant not having listened —
    #     "summarise the latest news from the web" opened an upload panel the
    #     user had nothing to put in.
    if (
        kb_id is None
        and not state.get("docs_asked")
        and bs.needs_documents(agent_type, purpose, str(filled.get("content", "")))
    ):
        state["docs_asked"] = True
        return ToolResult(
            content=(
                "Opened the upload panel so the agent is grounded in the user's real "
                "documents. Their next message reports what arrived — wait for it."
            ),
            summary="Asked for documents",
            ui={
                "command": "open_modal",
                "modal": "upload",
                "prompt": (
                    "Share the documents this agent should use — or skip to build without them."
                ),
            },
        )

    # 2. Ask the next must_ask slot still missing for this type. A destination that the
    #    Connect resource picker captures (Slack's channel) is skipped so we don't ask
    #    it twice — but picker-less connectors (gmail, google-sheets) keep their slot:
    #    the interview is the ONLY place their target is captured.
    slots = bs.missing_slots(
        agent_type, filled, integrations, skip_destination_for=_picker_captured(integrations)
    )
    if slots:
        # Ask EVERYTHING in one sheet (a multi-step wizard) rather than one popup per
        # slot — the WS records each answer by its slot id on resume. Wording is
        # LLM-personalised to this goal (canonical fallback) so sheets don't repeat.
        return _slots_ask(
            slots,
            lead="Before I build this, a few things I need to get right —",
            phrased=await _phrase_questions(purpose, slots),
        )

    # 3. Everything's filled — show a real dry-run sample (nothing is created or posted),
    #    reflect the spec back, and require explicit confirmation.
    if not state.get("confirmed"):
        state["asked"] = _CONFIRM_SLOT
        # The interview's name answer (the change path) wins over the args name.
        effective_name = str(filled.get("name") or name).strip()[:60] or name
        spec = bs.reflect_spec(effective_name, agent_type, filled, integrations)
        spec_slots = _spec_slots(effective_name, agent_type, filled, integrations)
        # One confirm request_id, shared by the preview block + the ask_user interrupt, so
        # the preview can render the Accept/Adjust buttons (accept-in-preview).
        confirm_rid = str(state.setdefault("confirm_rid", str(uuid.uuid4())))
        if kb_id and not state.get("preview_tried"):
            state["preview_tried"] = True  # attempt the dry-run once (best-effort)
            state["preview_shown"] = await _emit_preview(
                ctx,
                kb_id,
                model,
                filled,
                spec=spec,
                spec_slots=spec_slots,
                request_id=confirm_rid,
            )
        # The confirm lives ON an inline block in the chat, never a popup: the preview
        # block when a dry-run exists, else a plain spec block ("What I'll build") —
        # the ask_user still fires (the pause), tagged with the shared id, and the
        # frontend suppresses the sheet. The sheet only appears as a last-resort
        # fallback (no stream writer, e.g. the legacy loop path).
        block_shown = bool(state.get("preview_shown")) or _emit_confirm_block(
            spec, spec_slots=spec_slots, request_id=confirm_rid
        )
        question = (
            "Ready to build it?"
            if block_shown
            else f"Here's what I'll build — {spec}  Shall I build it?"
        )
        return _slot_ask(
            _CONFIRM_SLOT,
            question,
            ["Yes, build it", "Let me change something"],
            lead="Reflected the spec and asked the user to confirm — wait for their answer.",
            request_id=confirm_rid,
        )

    return None  # complete + confirmed → proceed to build


_SLOT_LABELS = {
    "content": "Content",
    "schedule": "Schedule",
    "destination": "Slack channel",
    "email_to": "Email to",
    "gmail_to": "Send to (Gmail)",
    "outlook_to": "Send to (Outlook)",
    "teams_channel": "Teams channel",
    "excel_link": "Excel file",
    "sheet_id": "Google Sheet",
    "chat_space": "Chat space",
    "empty_behavior": "When there's nothing new",
    "out_of_scope": "Out of scope",
    "escalation": "Escalates",
    "steps": "Steps",
    "success": "Success looks like",
}

_TYPE_LABELS = {
    "knowledge": "Knowledge / Q&A",
    "scheduled_publisher": "Scheduled publisher",
    "monitor": "Monitor / trigger",
    "workflow": "Workflow automation",
}


def _spec_slots(
    name: str, agent_type: str, filled: dict[str, Any], integrations: list[str]
) -> list[dict[str, Any]]:
    """The reflected spec as STRUCTURED rows for the preview card — the UI renders a
    clean labeled grid and edits rows inline (id = the interview slot the edited
    value records back into). Readonly rows aren't requirements. O(slots)."""
    from sila.services.sila import build_slots as bs

    rows: list[dict[str, Any]] = [
        {"id": "name", "label": "Name", "value": name or "Agent"},
        {
            "id": "__type__",
            "label": "Type",
            "value": _TYPE_LABELS.get(agent_type, agent_type),
            "readonly": True,
        },
    ]
    for slot in bs.REQUIRED_SLOTS.get(agent_type, ()):
        if slot.name == "name":
            continue  # shown first, from the effective name
        val = filled.get(slot.name) or (slot.default if not slot.must_ask else None)
        if not val:
            continue
        row: dict[str, Any] = {
            "id": slot.name,
            "label": _SLOT_LABELS.get(slot.name, slot.name.replace("_", " ").capitalize()),
            "value": str(val),
        }
        if slot.name == "schedule":
            row["kind"] = "schedule"
        rows.append(row)
    if integrations:
        rows.append(
            {
                "id": "__integrations__",
                "label": "Integrations",
                "value": ", ".join(integrations),
                "readonly": True,
            }
        )
    return rows


def _emit_confirm_block(
    spec: str, *, spec_slots: list[dict[str, Any]] | None = None, request_id: str
) -> bool:
    """No dry-run preview available → emit the reflected spec as an inline confirm
    block in the trace (the 'What I'll build' card carrying Accept/Adjust), so the
    confirmation reads as part of the conversation, not a popup. Best-effort: no
    stream writer (loop path / tests) → False, and the sheet fallback carries it."""
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(
            {
                "type": "ui",
                "command": "open_preview",
                "kind": "confirm",
                "output": spec,
                "destination": None,
                "spec_slots": spec_slots or [],
                "request_id": request_id,
            }
        )
        return True
    except Exception:  # noqa: BLE001 — fall back to the docked sheet
        return False


def _preview_destination(filled: dict[str, Any]) -> str | None:
    """A short 'where it would go' label for the preview card (best-effort)."""
    from sila.services.sila.build_slots import normalize_destination

    ch = str(filled.get("slack_channel") or filled.get("channel") or "").strip()
    if ch:
        return ch if ch.startswith("#") else f"#{ch}"
    email = str(filled.get("email_to") or filled.get("gmail_to") or "").strip()
    if email:
        return email
    sheet = str(filled.get("sheet_id") or "").strip()
    if sheet:
        sid = normalize_destination("sheet_id", sheet)
        return f"Google Sheet {sid[:12]}…" if len(sid) > 12 else f"Google Sheet {sid}"
    space = str(filled.get("chat_space") or "").strip()
    if space:
        sid = normalize_destination("chat_space", space)
        return f"Google Chat {sid[:18]}…" if len(sid) > 18 else f"Google Chat {sid}"
    return None


async def _emit_preview(
    ctx: ToolContext,
    kb_id: str,
    model: str,
    filled: dict[str, Any],
    *,
    spec: str,
    spec_slots: list[dict[str, Any]] | None = None,
    request_id: str,
) -> bool:
    """Generate ONE real sample from the user's KB and emit it as a preview block into
    the trace — the 'show me before you build it' moment. The block carries the spec +
    the confirm request_id, so it renders the Accept/Adjust buttons itself (the confirm
    interrupt is tagged with the same id). The generation is a real LLM call (can take
    ~a minute on a small model), so it runs under a VISIBLE step — never dead air.
    Best-effort: any failure is swallowed so it never blocks the build. Returns True
    only if a sample was shown."""
    from sila.services.sila.preview import generate_sample

    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:  # noqa: BLE001 — no stream writer (loop path / tests): the
        return False  # sheet fallback carries the spec; skip the costly generation.

    from sila.services.sila.build_slots import detect_goal_language
    from sila.services.sila.workflow_assembly import summary_instruction_for

    content = str(filled.get("content", "")).strip()
    # Same instruction the built workflow will use — the preview must show the
    # real output, including the conversation-derived language.
    instruction = summary_instruction_for(
        content, detect_goal_language(str(ctx.build_state.get("goal") or ""))
    )
    writer({"type": "tool", "name": "generate_preview", "status": "running"})
    try:
        sample = await generate_sample(
            db=ctx.db,
            workspace_id=ctx.workspace_id,
            kb_id=uuid.UUID(kb_id),
            instruction=instruction,
            model=model,
            query=content or "overview key points policies",
        )
    except Exception:  # noqa: BLE001 — preview is best-effort, never fail the build
        sample = ""
    if not sample:
        writer({"type": "tool", "name": "generate_preview", "status": "done", "result": "skipped"})
        return False
    writer({"type": "tool", "name": "generate_preview", "status": "done", "result": "sample ready"})
    writer(
        {
            "type": "ui",
            "command": "open_preview",
            "output": sample,
            "destination": _preview_destination(filled),
            "spec": spec,
            "spec_slots": spec_slots or [],
            "request_id": request_id,
        }
    )
    return True


async def _validated_model(model: str) -> str:
    """Only ship a model that's actually installed. The LLM sometimes HALLUCINATES a
    model name into the build args (e.g. 'text-adalm-3b'), which would bake a dead LLM
    node into the workflow. Verify against Ollama's installed tags; fall back to the
    platform default on a mismatch or if we can't check. O(installed)."""
    default = settings.sila_llm_model
    if not model or model == default:
        return default
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.ollama_url}/api/tags")
            resp.raise_for_status()
            installed = {m.get("name") for m in resp.json().get("models", [])}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return default  # can't verify → safe default, never a hallucinated name
    return model if model in installed else default


def _schedule_to_cron(answer: str) -> str:
    """Map a captured schedule-slot answer to a cron expression (default 9am daily).
    The time-wheel picker sends a real cron (5 fields) → pass it through. Phrases
    parse the time too ('weekdays only at 5pm' → 0 17 * * 1-5; '7:30' → 30 7),
    so a typed correction never silently reverts to 9am."""
    a = answer.strip()
    parts = a.split()
    if len(parts) == 5 and all(c in "0123456789*/,-" for c in a.replace(" ", "")):
        return a  # already a cron from the picker
    al = a.lower()
    hour, minute = 9, 0
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", al)
    if m and (m.group(2) is not None or m.group(3)):
        # Only trust a number that carries :mm or am/pm — a bare digit is ambiguous.
        raw_hour = int(m.group(1))
        meridiem = m.group(3)
        hour = (raw_hour % 12) + (12 if meridiem == "pm" else 0) if meridiem else raw_hour
        minute = int(m.group(2) or 0)
        hour, minute = min(hour, 23), min(minute, 59)
    if "weekday" in al:
        return f"{minute} {hour} * * 1-5"
    if "weekend" in al:
        return f"{minute} {hour} * * 0,6"
    if "week" in al or "monday" in al:
        return f"{minute} {hour} * * 1"
    return f"{minute} {hour} * * *"


async def _create_agent_workflow(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Build a real (inactive) n8n RAG workflow and register it as a Draft agent."""
    name = (str(args.get("name") or "")).strip()[:60] or "Custom Agent"
    purpose = str(args.get("purpose") or "").strip()
    model = await _validated_model(str(args.get("model") or settings.sila_llm_model).strip())
    kb_id = str(args.get("kb_id") or "").strip() or None
    raw = args.get("integrations")
    integrations = [str(i) for i in raw] if isinstance(raw, list) else []

    # The user's OWN WORDS are the purpose. A weak model repeatedly re-imagines the
    # request when passing args (an HR Q&A ask became the prompt's daily-Slack
    # example), which then mis-classifies the whole interview — so the graph pins the
    # original request (build_state["goal"]) and it wins on the first stash.
    goal = str(ctx.build_state.get("goal") or "").strip()
    if ctx.interactive and goal and not isinstance(ctx.build_state.get("build_args"), dict):
        purpose = goal

    # An existing stash (seeded by an accepted plan, or the first call's args) is
    # AUTHORITATIVE for name/purpose — a weak model re-calling this tool must not
    # reinvent what the user already agreed to (the hallucinated-handoff bug).
    # Integrations union so the model can still ADD one; kb_id keeps whichever is set.
    stash = ctx.build_state.get("build_args")
    if ctx.interactive and isinstance(stash, dict):
        name = str(stash.get("name") or name).strip()[:60]
        purpose = str(stash.get("purpose") or purpose).strip()
        if isinstance(stash.get("integrations"), list):
            merged = {str(i).lower() for i in stash["integrations"]}
            merged.update(str(i).lower() for i in integrations)
            integrations = sorted(merged)
        kb_id = kb_id or (str(stash.get("kb_id") or "").strip() or None)

    # Connectors the user NAMED override the model's integration guess (deterministic
    # manifest-token match): "via Gmail" must wire the Gmail connector — the model
    # once mapped it to the generic platform email (→ MailHog in dev, never Gmail).
    from sila.services.connectors.manifest import providers_named_in

    named = providers_named_in(goal or purpose)
    if named:
        integ_set = {i.lower() for i in integrations}
        if named & {"gmail", "outlook-mail"}:
            # They named a mail CONNECTOR — the model's generic platform-email
            # guess would double-send (and land in MailHog in dev).
            integ_set.discard("email")
        integrations = sorted(integ_set | named)

    # Idempotency guard: a weak model sometimes re-calls build AFTER it already
    # succeeded, creating duplicate agents. If this name was already built this
    # connection, refuse terminally (no UI → the loop ends), don't build a second copy.
    if name in ctx.build_state.get("built_names", []):
        return ToolResult(
            content=(
                f"'{name}' is already built — it's in your Agents as a Draft, pending "
                "review. To build a different agent tell me what it should do; to change "
                "this one tell me what to adjust."
            ),
            summary="Already built",
        )

    # Stash the build args so the graph can re-invoke this tool deterministically after
    # each slot answer (the pause node re-runs it) — the interview must NOT depend on a
    # weak model re-calling build. Set once (the first call's args win); cleared on a
    # successful registration below.
    if ctx.interactive:
        ctx.build_state.setdefault(
            "build_args",
            {
                "name": name,
                "purpose": purpose,
                "model": model,
                "kb_id": kb_id,
                "integrations": integrations,
            },
        )
        # A kb that resolved THIS call (upload finished) must survive the
        # deterministic pause re-runs — sync it into the stash.
        if kb_id and not ctx.build_state["build_args"].get("kb_id"):
            ctx.build_state["build_args"]["kb_id"] = kb_id

    # "Let me change something": the model relays the user's corrections as `slots`
    # ({requirement: new value}); the gate merges them (whitelisted per type) and —
    # because the spec changed — re-confirms with a FRESH preview + confirm id (a
    # stale id would pin the buttons to the outdated preview block).
    if ctx.interactive:
        from sila.services.sila import build_slots as bs

        at = ctx.build_state.get("agent_type") or bs.classify_agent_type(purpose, integrations)
        if bs.merge_slot_overrides(ctx.build_state, at, args.get("slots")):
            for stale in ("confirm_rid", "preview_tried", "preview_shown"):
                ctx.build_state.pop(stale, None)
            ctx.build_state["confirmed"] = False

    # Part B — DETERMINISTIC requirements gate. A weak model won't self-gate from the
    # prompt, so the build is BLOCKED here until the interview is complete: the agent
    # type is classified, every must_ask slot for that type is filled, the destination
    # for any wired connector is captured, AND the user confirmed the reflected spec.
    # Each return below is a pausing ask_user; on resume the pause node re-runs this
    # tool with the stashed args and the gate advances (no model re-call needed).
    gate = await _requirements_gate(ctx, name, purpose, integrations, kb_id, model)
    if gate is not None:
        return gate

    # The interview's answers — destinations for picker-less connectors (gmail
    # recipient, sheet id) are injected into the connector node data from here,
    # and a name captured/changed in the interview WINS over the args name (it
    # was reflected + confirmed; building under the old name would be a lie).
    filled: dict[str, Any] = ctx.build_state.get("filled") or {}
    name = str(filled.get("name") or name).strip()[:60] or name

    # WS-9 — OAuth connectors (Slack, …) authorize through Nango, not a typed
    # token. If the user hasn't connected one yet, pause and open the Connect
    # flow; otherwise resolve it to a proxy node (n8n holds no token). This runs
    # BEFORE the B2 credential gate so OAuth providers skip the Incognito panel.
    connector_nodes, connector_token, connect_interrupt = await _resolve_oauth_connectors(
        ctx, integrations, filled
    )
    if connect_interrupt is not None:
        return connect_interrupt

    # Auth-needing NON-OAuth integrations (raw SMTP/email) MUST have a typed
    # credential first (the B2 Incognito path). If one is missing, don't build
    # yet — open the secure masked panel; the build retries once it's connected.
    from sila.services.sila.credential_service import fields_payload, spec_for

    for integ in integrations:
        if integ in connector_nodes or integ == "email":
            continue  # slack → Nango above; email → platform SMTP (no user credential)
        cspec = spec_for(integ)
        if cspec and cspec.target == "n8n" and integ not in ctx.pending_credentials:
            return ToolResult(
                content=(
                    f"Before I can wire {cspec.label} into this agent I need its access "
                    "token. Opening a secure, masked panel — once you submit it I'll finish "
                    "building. The value is never saved in this chat."
                ),
                summary=f"Need {cspec.label} credentials",
                ui={
                    "command": "request_credentials",
                    "request_id": str(uuid.uuid4()),
                    "provider": cspec.provider,
                    "label": cspec.label,
                    "fields": fields_payload(cspec),
                },
            )

    pre_lines = [f"You are {name}, a helpful assistant for this organization."]
    if purpose:
        pre_lines.append(purpose)
    if kb_id:
        pre_lines.append(
            "Ground every answer in the retrieved knowledge; if it isn't there, say so."
        )
    pre_lines.append("Be clear, concise, and professional.")

    kb_search_url = (
        f"{settings.sila_api_internal_url}/v1/workspaces/{ctx.workspace_id}/kbs/{kb_id}/search"
        if kb_id
        else None
    )

    # Lazy imports avoid a service↔registry import cycle.
    from sila.adapters.agent_runtime.dify import DifyAdapter
    from sila.adapters.n8n.rest import N8nRestAdapter
    from sila.services import n8n_provisioning
    from sila.services.agent_service import AgentService
    from sila.services.n8n_workflow_builder import WorkflowSpec, build_rag_workflow
    from sila.services.vetting_service import VettingService

    # Wire in only the credentials collected this connection for these integrations.
    wired = {p: ref for p, ref in ctx.pending_credentials.items() if p in integrations}
    connector_proxy_url = (
        f"{settings.sila_api_internal_url}/v1/internal/connectors/proxy"
        if connector_nodes
        else None
    )
    # The RAG runs in Sila (the langchain node can't execute on n8n's main flow): mint
    # a capability + point the workflow at the internal agent-run endpoint.
    run_url: str | None = None
    run_token: str | None = None
    if kb_id:
        from sila.services.sila import run_capability

        run_url = f"{settings.sila_api_internal_url}/v1/internal/agents/run"
        run_token = run_capability.mint(
            workspace_id=ctx.workspace_id, kb_id=uuid.UUID(kb_id), model=model
        )

    # A scheduled publisher posts on its own → derive a cron from the captured schedule
    # slot; other agent types stay webhook-triggered (on-demand).
    schedule_cron = (
        _schedule_to_cron(str(filled.get("schedule", "")))
        if ctx.build_state.get("agent_type") == "scheduled_publisher"
        else None
    )
    from sila.services.sila.build_slots import detect_goal_language
    from sila.services.sila.workflow_assembly import summary_instruction_for

    content = str(filled.get("content", "")).strip()
    # Output language follows the user's own words (deterministic) — unstated, a
    # small model picks one at random (an English ask once produced an Arabic digest).
    language = detect_goal_language(goal or purpose)
    summary_instruction = summary_instruction_for(content, language)

    # Email destination — platform SMTP (sovereign): mint a capability bound to the
    # captured recipient (the build interview's required email_to slot; falls back to
    # the user's own address). The SMTP send runs in Sila; n8n holds no creds.
    email_url: str | None = None
    email_token: str | None = None
    if "email" in integrations:
        from sila.services.sila import email_capability

        recipient = str(filled.get("email_to") or "").strip() or ctx.user.email
        email_url = f"{settings.sila_api_internal_url}/v1/internal/agents/send-email"
        email_token = email_capability.mint(
            workspace_id=ctx.workspace_id, user_id=ctx.user.id, to=recipient
        )

    spec = WorkflowSpec(
        name=name,
        pre_prompt="\n".join(pre_lines),
        model=model,
        ollama_url=settings.ollama_url,
        kb_search_url=kb_search_url,
        run_url=run_url,
        run_token=run_token,
        summary_instruction=summary_instruction,
        email_url=email_url,
        email_token=email_token,
        schedule_cron=schedule_cron,
        integrations=integrations,
        credentials=wired,
        connectors=connector_nodes,
        connector_proxy_url=connector_proxy_url,
        connector_token=connector_token,
    )
    definition = build_rag_workflow(spec)

    # Create the INACTIVE workflow in the user's own n8n studio session.
    n8n = N8nRestAdapter(settings.n8n_internal_url)
    try:
        session = await n8n_provisioning.ensure_session(
            db=ctx.db, n8n=n8n, supabase_user_id=ctx.user.id, sila_full_name=None
        )
        workflow_id = await n8n.create_workflow(session.cookie_value, definition)
    except Exception as exc:  # noqa: BLE001 — surface a polite failure to the model
        log.warning("sila.create_workflow.failed", error=str(exc))
        return ToolResult(content=f"Couldn't create the workflow: {exc}", summary="Build failed")

    # Register as a Draft + Unvetted workflow agent, then submit to the gatekeeper.
    # Assemble the editable publisher config — the workspace card + the safe-fields edit
    # path read this (distinct from the n8n definition, which is rebuilt FROM it on edit).
    from sila.db.repositories.connectors import ConnectorBindingRepository

    destinations: list[dict[str, Any]] = []
    if "slack" in connector_nodes:
        chan_id = str(connector_nodes["slack"].data.get("channel") or "")
        chan_name = chan_id
        binding = await ConnectorBindingRepository(ctx.db).get(
            user_id=ctx.user.id, workspace_id=ctx.workspace_id, provider="slack"
        )
        if binding and isinstance(binding.selected_resources, dict):
            chan_name = str(
                (binding.selected_resources.get("channel") or {}).get("label") or chan_id
            )
        destinations.append({"type": "slack", "channel_id": chan_id, "channel_name": chan_name})
    if "email" in integrations:
        destinations.append({"type": "email", "to": str(filled.get("email_to") or ctx.user.email)})
    # Picker-less connector destinations (Google) — read back from the node data,
    # which holds only config fields (static + the normalized interview target),
    # so it maps 1:1 onto an editable destination entry.
    for provider, node in connector_nodes.items():
        if provider == "slack":
            continue  # handled above (needs the channel-name lookup)
        entry: dict[str, Any] = {"type": provider, **node.data}
        if provider == "gmail" and not entry.get("to"):
            entry["to"] = ctx.user.email  # "email me" fallback — send to the builder
        destinations.append(entry)
    docs: list[str] = []
    if kb_id:
        from sqlalchemy import select

        from sila.db.models import KBDocument

        rows = await ctx.db.execute(
            select(KBDocument.filename).where(KBDocument.kb_id == uuid.UUID(kb_id))
        )
        docs = [str(f) for f in rows.scalars().all() if f]
    publisher_config: dict[str, Any] = {
        "schedule": {"cron": schedule_cron, "timezone": "Asia/Muscat"} if schedule_cron else None,
        "destinations": destinations,
        "kb_id": kb_id,
        "docs": docs,
        "content": content or None,
        "model": model,
        "language": language,  # the edit-path rebuild keeps the output language
    }
    from sila.services.sila.workflow_assembly import destination_label

    dest_label = " and ".join(destination_label(d) for d in destinations)
    brief = (
        f"{content or 'Summarizes your documents'} — posts to {dest_label}."
        if destinations
        else (content or None)
    )

    service = AgentService(ctx.db, DifyAdapter())
    try:
        agent = await service.register_workflow_agent(
            workspace_id=ctx.workspace_id,
            owner_id=ctx.user.id,
            workflow_id=workflow_id,
            definition=definition,
            config=publisher_config,
            brief=brief,
        )
    except HTTPException as exc:
        return ToolResult(
            content=f"Couldn't register the agent: {exc.detail}", summary="Build failed"
        )

    agent_id = str(agent.id)
    ui = {"command": "open_canvas", "workflow_id": workflow_id, "definition": definition}

    # Build succeeded — reset the interview so a NEXT build starts fresh, but REMEMBER
    # the name so a weak model re-calling build doesn't create a duplicate (the
    # idempotency guard at the top of this tool).
    built = [*ctx.build_state.get("built_names", []), name]
    ctx.build_state.clear()
    ctx.build_state["built_names"] = built

    if ctx.defer_vetting:
        # Graph path: hand the agent to the vetting node, which streams the review
        # as visible chat steps. Don't submit here (avoids a double scan).
        return ToolResult(
            content=(
                f"Created draft agent '{name}' (id={agent_id}) backed by an inactive n8n "
                f"workflow ({'with' if kb_id else 'without'} a knowledge base). It's Draft + "
                "Unvetted; running the security review now."
            ),
            summary=f"Created draft agent: {name}",
            ui=ui,
            # `agent` rides the frame so the trace can offer a link straight to
            # what was just built. Previously the id existed only inside prose
            # the model may or may not repeat, so there was nothing to click.
            meta={
                "vetted_agent_id": agent_id,
                # `integrations` rides along so the attachment card can show the
                # agent's actual connectors (Slack, Gmail, …) instead of a
                # generic robot glyph that tells the user nothing.
                "artifacts": [
                    _artifact(
                        "agent",
                        name,
                        id=str(agent_id),
                        integrations=list(integrations or []),
                        web=bs.is_web_sourced(purpose, str(filled.get("content", ""))),
                        security=await _agent_gate(purpose, filled),
                    )
                ],
            },
        )

    # Loop path: submit to the gatekeeper in the background as before.
    security = await _agent_gate(purpose, filled)
    background = BackgroundTasks()
    try:
        await VettingService(ctx.db).submit_for_review(
            agent=agent, actor_id=ctx.user.id, background_tasks=background
        )
    except HTTPException as exc:  # registered as Draft even if the scan can't start
        log.warning("sila.submit_for_review.failed", error=str(exc.detail))
    await background()

    return ToolResult(
        content=(
            f"Created draft agent '{name}' (id={agent_id}) backed by an inactive n8n workflow "
            f"({'with' if kb_id else 'without'} a knowledge base). It's Draft + Unvetted and is "
            "now in the security review; a human approves before it can be published."
        ),
        summary=f"Created draft agent: {name}",
        ui=ui,
        meta={
            "artifacts": [
                _artifact(
                    "agent",
                    name,
                    id=str(agent_id),
                    integrations=list(integrations or []),
                    web=bs.is_web_sourced(purpose, str(filled.get("content", ""))),
                    security=security,
                )
            ]
        },
    )


async def _agent_gate(purpose: str, filled: dict[str, Any]) -> dict[str, Any]:
    """The gateway on a draft agent's own words — its purpose and every
    interview answer. The Phase-6 review still runs; this is what makes the
    verdict visible on the card the user is looking at, right now."""
    from sila.services.sila.deliverable_guard import agent_chunks, guard_chunks

    return await guard_chunks(agent_chunks(purpose, filled))


async def _okr_build_pack(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        from sila.services.sila import okr_tool
    except ImportError:  # an edition shipped without the OKR pack
        return ToolResult(
            content="The OKR Reporting Pack is not part of this edition.",
            summary="OKR pack unavailable",
        )
    return await okr_tool.run(ctx, args)


TOOLS: dict[str, SilaTool] = {
    t.name: t
    for t in (
        SilaTool(
            name="okr_build_pack",
            description=(
                "OKR Reporting Pack: build the monthly deck (on the GRC template) and "
                "the dashboard from the OKR sheet the user attached (.xlsx or .csv). "
                "Validates each progress update on the local model. Returns the files "
                "as download cards and a JOB RESULT to relay as written."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["en", "ar"],
                        "description": (
                            "What Sila writes in the deck. 'en' unless the user "
                            "explicitly asked for Arabic."
                        ),
                    },
                    "file": {
                        "type": "string",
                        "description": "Part of the attached file's name, if several.",
                    },
                },
            },
            min_role=Role.VIEWER,
            run=_okr_build_pack,
        ),
        SilaTool(
            name="ask_user",
            description=(
                "Ask the user 1–3 CONSEQUENTIAL multiple-choice questions as a tappable "
                "wizard — only ones that change the build or carry risk (e.g. what an agent "
                "should do when the answer isn't in its documents, or which topics it must "
                "escalate to a human). Don't ask low-stakes things (name, tone, language) — "
                "pick sensible defaults. Each question needs a short `question` and an "
                "`options` list; set `select` to 'single_select'|'multi_select', and "
                "`allow_other` to true so the user can type their own answer. Answers arrive "
                "in the next message. For open-ended things (a name) just ask in plain text."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "question": {"type": "string"},
                                "select": {
                                    "type": "string",
                                    "enum": ["single_select", "multi_select"],
                                },
                                "options": {"type": "array", "items": {"type": "string"}},
                                "allow_other": {"type": "boolean"},
                            },
                            "required": ["question", "options"],
                        },
                    }
                },
                "required": ["questions"],
            },
            min_role=Role.VIEWER,
            run=_ask_user,
        ),
        SilaTool(
            name="create_agent_workflow",
            description=(
                "Build the agent as a real (inactive) n8n RAG workflow and register it as a "
                "Draft. Call this AFTER open_agent_builder once the user has submitted the "
                "spec. Pass the name, purpose, the chosen model, the kb_id (if they uploaded "
                "documents), and any integrations. Everything stays Draft + Unvetted."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short agent name."},
                    "purpose": {
                        "type": "string",
                        "description": (
                            "The user's COMPLETE goal in their own words — INCLUDING any "
                            "posting, scheduling, notification, or monitoring intent. Do not "
                            "summarize that part away: write 'answer HR questions from the "
                            "handbook AND post a daily summary to Slack', not just 'answer HR "
                            "questions'. The build tool classifies the agent type from this, "
                            "so a dropped 'daily/post/notify' means the wrong questions."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": "The Ollama model the agent should run on.",
                    },
                    "kb_id": {
                        "type": "string",
                        "description": "Knowledge-base id from the form, if docs were given.",
                    },
                    "integrations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional integrations, e.g. ['email','slack'].",
                    },
                    "slots": {
                        "type": "object",
                        "description": (
                            "ONLY when the user asked to change something after the "
                            "confirmation: the corrections as {requirement: new value} — "
                            "keys like schedule, content, destination, email_to, steps, "
                            "out_of_scope, escalation, name. Pass ONLY what changed; "
                            "everything else is kept."
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["name", "purpose"],
            },
            min_role=Role.BUILDER,
            run=_create_agent_workflow,
        ),
        SilaTool(
            name="request_documents",
            description=(
                "Open an upload panel so the user can give you documents (e.g. an HR "
                "handbook) to ground an agent you're building. Call this BEFORE building "
                "an agent that needs the user's own files; their next message reports "
                "what they uploaded."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Short instruction shown in the upload panel.",
                    }
                },
                "required": [],
            },
            min_role=Role.VIEWER,
            run=_request_documents,
        ),
        SilaTool(
            name="request_credentials",
            description=(
                "Open a masked, private panel for the user to enter a secret (API key / "
                "token) for an integration you're wiring into an agent — e.g. a Slack bot "
                "token or SMTP login. Use this WHENEVER an integration needs auth: NEVER ask "
                "the user to type a secret in the chat. `provider` must be one of: slack, "
                "email, mcp. The value is stored encrypted and never appears in the "
                "conversation; the user's confirmation arrives next — wait, then build."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["slack", "email", "mcp"]},
                },
                "required": ["provider"],
            },
            min_role=Role.BUILDER,
            run=_request_credentials,
        ),
        SilaTool(
            name="propose_plan",
            description=(
                "Present a structured PLAN for an agent as an approvable card — what it "
                "would do, its type, the documents it needs, destination + integrations, "
                "schedule, and fallback behaviour. Call search_bazaar FIRST and pass any "
                "ready-made match as bazaar_match. The user then Accepts (the build "
                "starts, pre-filled), Adjusts, or Discards — you never build. Include "
                "open_decisions for anything the user still has to choose."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "One clear sentence: what the agent would do.",
                    },
                    "name": {
                        "type": "string",
                        "description": "A short name for the agent this plan would build.",
                    },
                    "agent_type": {
                        "type": "string",
                        "enum": ["knowledge", "scheduled_publisher", "monitor", "workflow"],
                    },
                    "content": {
                        "type": "string",
                        "description": "What each output/post actually contains.",
                    },
                    "sources": {
                        "type": "string",
                        "description": "The documents/data it needs from the user.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Where results go (a channel, an email address).",
                    },
                    "integrations": {"type": "array", "items": {"type": "string"}},
                    "schedule": {
                        "type": "string",
                        "description": "When it runs, incl. time + timezone + days.",
                    },
                    "fallback": {
                        "type": "string",
                        "description": "Behaviour when the answer isn't in the documents.",
                    },
                    "escalation": {
                        "type": "string",
                        "description": "Topics that must go to a human instead.",
                    },
                    "steps": {
                        "type": "array",
                        "description": (
                            "The execution decomposition, in order. Titles must be "
                            "functional and context-driven (what the step DOES with which "
                            "data/system) — never 'Step 1' or 'Phase A'. Note dependencies "
                            "and which steps can run in parallel."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "detail": {"type": "string"},
                                "input": {
                                    "type": "string",
                                    "description": "The data this step consumes (its shape).",
                                },
                                "output": {
                                    "type": "string",
                                    "description": "The data this step hands to the next.",
                                },
                                "depends_on": {
                                    "type": "string",
                                    "description": "Which earlier step(s) it needs first.",
                                },
                                "parallel": {
                                    "type": "boolean",
                                    "description": "True if it can run alongside siblings.",
                                },
                            },
                            "required": ["title"],
                        },
                    },
                    "risks": {
                        "type": "array",
                        "description": (
                            "What can go wrong and the CONCRETE fallback (e.g. invalid "
                            "model output → re-prompt once with the error; a blocked "
                            "source → skip and flag it in the result)."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "risk": {"type": "string"},
                                "fallback": {"type": "string"},
                            },
                            "required": ["risk", "fallback"],
                        },
                    },
                    "checkpoints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Human-in-the-loop safety checkpoints — where a person must "
                            "approve before the workflow proceeds (e.g. approving drafted "
                            "emails before anything is sent)."
                        ),
                    },
                    "open_decisions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "options": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["question"],
                        },
                    },
                    "bazaar_match": {
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "string"},
                            "name": {"type": "string"},
                            "note": {"type": "string"},
                        },
                        "required": ["agent_id", "name"],
                    },
                },
                "required": ["goal"],
            },
            min_role=Role.VIEWER,
            run=_propose_plan,
        ),
        SilaTool(
            name="navigate",
            description=(
                "Open a page of the platform for the user. Use when they ask to go "
                "somewhere or after an action whose result lives on that page."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "page": {"type": "string", "enum": list(_NAV_PATHS)},
                },
                "required": ["page"],
            },
            min_role=Role.VIEWER,
            run=_navigate,
        ),
        SilaTool(
            name="search_bazaar",
            description="Search the marketplace of published, security-vetted agents.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            min_role=Role.VIEWER,
            run=_search_bazaar,
        ),
        SilaTool(
            name="fetch_url",
            description=(
                "Fetch a public web page the user names (a pasted link) and read its "
                "text, so you can summarize it, extract facts, or answer questions "
                "about it. Use this whenever the user gives a URL and asks you to do "
                "something with its content. Read-only; static HTML only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The http(s) URL to fetch."}
                },
                "required": ["url"],
            },
            min_role=Role.VIEWER,
            run=_fetch_url,
        ),
        SilaTool(
            name="web_search",
            description=(
                "Search the web for current information you don't already have — news, "
                "facts, docs, prices, 'who/what/when' questions. Returns the top "
                "results (title, link, snippet). Use it when the user asks about "
                "something recent or external and did NOT give a URL; then, if needed, "
                "call fetch_url on the most relevant result to read the full page."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to search for."}},
                "required": ["query"],
            },
            min_role=Role.VIEWER,
            run=_web_search,
        ),
        SilaTool(
            name="remember",
            description=(
                "Save ONE durable fact about the user to their cross-chat memory — "
                "their role, a lasting preference ('always use medians', 'prefers "
                "Arabic'), or a correction they gave you. Use it the moment they state "
                "something worth keeping; keep the fact to one short sentence. Never "
                "store secrets, passwords, or one-off details."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "One short sentence to keep."}
                },
                "required": ["fact"],
            },
            min_role=Role.VIEWER,
            run=_remember,
        ),
        SilaTool(
            name="set_objective",
            description=(
                "Store what a data analysis is FOR — the business question, the "
                "decision it informs, the audience, and the metrics that matter. Call "
                "it when the user states a goal for their data ('which regions drag "
                "Q2 revenue?') or answers your 'what is this analysis for?' question. "
                "Later charts and findings are ranked around it. Do NOT interview a "
                "user who just wants a dashboard — one question max."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The business question, in the user's words.",
                    },
                    "decision": {
                        "type": "string",
                        "description": "The decision this analysis informs (optional).",
                    },
                    "audience": {
                        "type": "string",
                        "enum": ["exec", "analyst", "ops"],
                        "description": "Who reads the result (default exec).",
                    },
                    "time_grain": {
                        "type": "string",
                        "description": "Time grain if temporal (e.g. 'day', 'month').",
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The success metric column names, if named.",
                    },
                },
                "required": ["question"],
            },
            min_role=Role.VIEWER,
            run=_set_objective,
        ),
        SilaTool(
            name="propose_cleaning",
            description=(
                "Scan the attached data file's quality over the FULL file — missing "
                "values, duplicate rows, dates/numbers stored as text, category "
                "spelling variants — and propose numbered fixes. Call it when the "
                "user asks to check or clean their data, or before analysis on messy "
                "data. NOT for 're-run/refresh on new data' — that is "
                "refresh_analysis. NOTHING is applied; the user chooses."
            ),
            parameters={
                "type": "object",
                "properties": {"file": {"type": "string", "description": "Optional file name."}},
            },
            min_role=Role.VIEWER,
            run=_propose_cleaning,
        ),
        SilaTool(
            name="apply_cleaning",
            description=(
                "Apply the cleaning fixes the USER chose from propose_cleaning's "
                "numbered list. Pass their numbers in `ops` (omit for all). The "
                "original data is kept — undo_cleaning reverts. Refuses to run "
                "without a prior proposal."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Optional file name."},
                    "ops": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "The chosen fix numbers (1-based). Omit for all.",
                    },
                    "extra_ops": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Explicit extra ops the user asked for by name: "
                            "{op:'drop_column',column}, {op:'impute',column,strategy}, "
                            "{op:'clip_outliers',column}, or "
                            "{op:'filter_rows',column,cmp,value}."
                        ),
                    },
                },
            },
            min_role=Role.VIEWER,
            run=_apply_cleaning,
        ),
        SilaTool(
            name="undo_cleaning",
            description=(
                "Revert the attached data file to its ORIGINAL uploaded content, "
                "removing every applied cleaning fix. Use when the user asks to "
                "undo the cleaning."
            ),
            parameters={
                "type": "object",
                "properties": {"file": {"type": "string", "description": "Optional file name."}},
            },
            min_role=Role.VIEWER,
            run=_undo_cleaning,
        ),
        SilaTool(
            name="analyze_data",
            description=(
                "Run ONE statistical analysis over the attached data file (the FULL "
                "file, computed in code) and get ground-truth findings to relay: "
                "'compare' (is a metric different across groups — auto-picks the "
                "right test), 'correlate' (which numbers move together), 'trend' "
                "(direction, shifts, unusual days over time), 'forecast' (baseline "
                "next 7 days), 'drivers' (what's associated with a target metric), "
                "'segments' (which categories explain the variation), "
                "'concentration' (how much the top groups dominate). Use it when "
                "the user asks WHY/WHAT-DRIVES/IS-IT-DIFFERENT questions, not for "
                "building dashboards."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "analysis": {
                        "type": "string",
                        "enum": list(_ANALYSIS_KINDS),
                        "description": "Which analysis to run.",
                    },
                    "metric": {"type": "string", "description": "The numeric column."},
                    "by": {"type": "string", "description": "The grouping column."},
                    "segment": {
                        "type": "string",
                        "description": "Optional control segment for 'compare'.",
                    },
                    "date": {"type": "string", "description": "The date column (trend/forecast)."},
                    "target": {"type": "string", "description": "Target metric for 'drivers'."},
                    "file": {"type": "string", "description": "Optional file name."},
                },
                "required": ["analysis"],
            },
            min_role=Role.VIEWER,
            run=_analyze_data,
        ),
        SilaTool(
            name="rank_insights",
            description=(
                "Rank everything analyze_data has found so far into the top insight "
                "cards (what happened → so what → confidence → caveats) plus a 3-5 "
                "bullet executive summary with one headline number. Call it when the "
                "user asks 'what matters', 'summarize the findings', or before "
                "presenting results to a manager."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "top_k": {
                        "type": "integer",
                        "description": "How many insight cards (default 5, max 10).",
                    }
                },
            },
            min_role=Role.VIEWER,
            run=_rank_insights,
        ),
        SilaTool(
            name="create_report_docx",
            description=(
                "Write the full analysis as a Word report (Objective, Data & "
                "Quality, Method, Findings, Recommendations, Dashboard figures, "
                "Appendix) — composed in code from this session's real results. "
                "Use when the user wants a document/report for a manager. Saves to "
                "OneDrive or hands back a download link."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Optional report title."}
                },
            },
            min_role=Role.VIEWER,
            run=_create_report_docx,
        ),
        SilaTool(
            name="create_insight_deck",
            description=(
                "Build a slide deck from the ranked insights — one insight per "
                "slide. ALWAYS ask the user first whether they want an 'executive' "
                "deck (management pitch, plain language) or a 'technical' one "
                "(full detail, confidence, caveats), then pass their choice as "
                "`style`. Requires rank_insights to have run (create_slide_deck "
                "stays for free-form decks)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "style": {
                        "type": "string",
                        "enum": ["executive", "technical"],
                        "description": "The user's chosen deck style — ask them first.",
                    },
                    "title": {"type": "string", "description": "Optional deck title."},
                    "template": {
                        "type": "string",
                        "description": "Optional workspace/builtin template name.",
                    },
                },
            },
            min_role=Role.VIEWER,
            run=_create_insight_deck,
        ),
        SilaTool(
            name="explain_chart",
            description=(
                "Explain ONE chart from the last built dashboard in plain language "
                "(English and Arabic returned; relay the user's language). Pass the "
                "chart's exact visible title. Use when the user asks what a chart "
                "means or how to read it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "The chart's visible title."}
                },
                "required": ["title"],
            },
            min_role=Role.VIEWER,
            run=_explain_chart,
        ),
        SilaTool(
            name="refresh_analysis",
            description=(
                "Re-run the whole known pipeline on the current data file: replays "
                "the saved cleaning recipe, checks DRIFT against the baseline "
                "(a schema change blocks the rebuild until the user confirms), "
                "evaluates alert rules, and rebuilds the dashboard. Use when the "
                "user uploads a new version of their data or asks to refresh/re-run."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Optional file name."},
                    "acknowledge_drift": {
                        "type": "boolean",
                        "description": (
                            "Pass true ONLY after the user explicitly confirmed "
                            "rebuilding on a changed schema."
                        ),
                    },
                },
            },
            min_role=Role.VIEWER,
            run=_refresh_analysis,
        ),
        SilaTool(
            name="set_alert_rule",
            description=(
                "Manage alert thresholds checked on every refresh — 'tell me if "
                "average churn goes above 5' → {metric:'churn', agg:'avg', "
                "cmp:'gt', threshold:5}. Actions: add (default), list, clear. "
                "Rules travel with the user's own agent copy."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "list", "clear"]},
                    "metric": {"type": "string", "description": "The column to watch."},
                    "agg": {
                        "type": "string",
                        "enum": ["avg", "sum", "min", "max", "count"],
                        "description": "How to aggregate it (default avg).",
                    },
                    "cmp": {
                        "type": "string",
                        "enum": ["gt", "lt", "ge", "le"],
                        "description": "Trigger when value compares like this.",
                    },
                    "threshold": {"type": "number", "description": "The threshold."},
                },
            },
            min_role=Role.VIEWER,
            run=_set_alert_rule,
        ),
        SilaTool(
            name="pdd_start",
            description=(
                "PDD template step 1: ingest the attached meeting transcript "
                "(.txt/.md) for a Process Design Document. Pass doc_type 'rpa' "
                "(software bot) or 'macro' (Excel macro) plus the process name. "
                "Returns the speaker roster to confirm."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "doc_type": {"type": "string", "enum": ["rpa", "macro"]},
                    "process_name": {"type": "string"},
                    "department": {"type": "string"},
                    "file": {"type": "string", "description": "Optional transcript file name."},
                    "reset": {
                        "type": "boolean",
                        "description": (
                            "Start over, discarding the existing session "
                            "(only on explicit user ask)."
                        ),
                    },
                },
                "required": ["doc_type"],
            },
            min_role=Role.VIEWER,
            run=_pdd_start,
        ),
        SilaTool(
            name="pdd_extract",
            description=(
                "PDD step 2: read the next part of the transcript (grounded "
                "extraction + validation). Keep calling until it reports done, "
                "then relay the quality report and blocking questions."
            ),
            parameters={"type": "object", "properties": {}},
            min_role=Role.VIEWER,
            run=_pdd_extract,
        ),
        SilaTool(
            name="pdd_answer_gap",
            description=(
                "PDD step 3: record the user's answer to one open question. "
                "gap_id accepts the gap id OR the section number ('1.3'). "
                "waive=true skips one question; waive_all=true skips every "
                "remaining question ('skip the rest'). Answers enter the "
                "document as user-provided content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "gap_id": {
                        "type": "string",
                        "description": "Gap id (gap-03) or section number (1.3).",
                    },
                    "answer": {"type": "string"},
                    "waive": {"type": "boolean"},
                    "waive_all": {"type": "boolean"},
                },
            },
            min_role=Role.VIEWER,
            run=_pdd_answer_gap,
        ),
        SilaTool(
            name="pdd_edit",
            description=(
                "PDD edits as TYPED patches (never prose): set_prose, set_cell, "
                "add_row, remove_row, add_item, remove_item, set_meta, set_raci "
                "{activity, person, letter}. Renaming steps syncs RACI + figures."
            ),
            parameters={
                "type": "object",
                "properties": {"patches": {"type": "array", "items": {"type": "object"}}},
                "required": ["patches"],
            },
            min_role=Role.VIEWER,
            run=_pdd_edit,
        ),
        SilaTool(
            name="pdd_export",
            description=(
                "PDD final step: quality report + export gate + pseudonymized "
                "Word document. Blocked while required questions are unanswered "
                "and unwaived. When the user says 'skip the questions and "
                "export' / 'export anyway', call with waive_open=true — never "
                "retry a blocked export unchanged."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "waive_open": {
                        "type": "boolean",
                        "description": (
                            "Waive every remaining question first (only when the "
                            "user explicitly said skip / export anyway)."
                        ),
                    }
                },
            },
            min_role=Role.VIEWER,
            run=_pdd_export,
        ),
        SilaTool(
            name="read_google_sheet",
            description=(
                "Read the user's Google Sheet (they paste a link or id) and attach its "
                "rows to this session as data, ready for a dashboard, slides, Power BI, "
                "or Grafana. Use whenever the user points at a Google Sheet."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sheet": {"type": "string", "description": "The Sheet URL or id."},
                    "range": {
                        "type": "string",
                        "description": "Optional A1 range (e.g. 'Sheet2!A1:F200').",
                    },
                },
                "required": ["sheet"],
            },
            min_role=Role.VIEWER,
            run=_read_google_sheet,
        ),
        SilaTool(
            name="read_outlook_emails",
            description=(
                "Read the user's OWN recent Outlook mail (their connected account) and "
                "attach it to this session — a chartable table (from/subject/received) "
                "plus the full text for summaries or documents. Use when they ask to "
                "analyze, summarize, or document their emails."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "refresh": {
                        "type": "boolean",
                        "description": (
                            "Fetch again even if this exact read was done earlier this "
                            "session. Default false: a repeat call returns the text already read."
                        ),
                    },
                    "folder": {
                        "type": "string",
                        "description": "Mail folder (default 'inbox').",
                    },
                    "search": {
                        "type": "string",
                        "description": "Optional search phrase (subject/body/sender).",
                    },
                    "since_days": {
                        "type": "integer",
                        "description": "How many days back (default 7).",
                    },
                    "limit": {"type": "integer", "description": "Max emails (default 25, ≤50)."},
                },
            },
            min_role=Role.VIEWER,
            run=_read_outlook_emails,
        ),
        SilaTool(
            name="read_email_attachments",
            description=(
                "Pull the FILE ATTACHMENTS off a recent Outlook email and attach them to "
                "this session (e.g. a CSV to clean/analyze, a text/JSON file). Picks the "
                "most recent email that has attachments; pass `subject` to target a "
                "specific one. Use when the user says 'the file in my email' / 'the "
                "attachment they sent'. Text/CSV attachments become usable immediately; "
                "Office/PDF binaries are captured with a note."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Subject phrase to match the email (optional).",
                    },
                    "folder": {"type": "string", "description": "Mail folder (default 'inbox')."},
                },
            },
            min_role=Role.VIEWER,
            run=_read_email_attachments,
        ),
        SilaTool(
            name="send_email",
            description=(
                "Send an email from the user's connected Outlook account. Call ONLY when "
                "the user explicitly asks to send/reply/email something (e.g. 'email the "
                "sender the link', 'reply with the results'). Sila wraps your message in a "
                "formal branded template (logo, signature, footer) — so write `body` as the "
                "message CONTENT ONLY: no greeting, no 'Best regards', no signature. Use "
                "Markdown for structure: a '## Heading' for the lead line, '### Sub-heading' "
                "for sections, '- ' bullets for lists, **bold** for emphasis. For the PRIMARY "
                "link (a dashboard/report), DON'T paste the raw URL in the body — pass it as "
                "`cta` so it renders as a button. This is an outbound action on the user's "
                "own mailbox."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address."},
                    "subject": {"type": "string", "description": "Email subject."},
                    "greeting": {
                        "type": "string",
                        "description": "Optional opening line, e.g. 'Hi Mojahid,'.",
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "The message content only, in Markdown (## / ### headings, "
                            "- bullets, **bold**) — no greeting, sign-off, or signature."
                        ),
                    },
                    "cta": {
                        "type": "object",
                        "description": (
                            "Optional primary link rendered as a button, e.g. "
                            '{"label": "View Dashboard", "url": "https://…"}.'
                        ),
                        "properties": {
                            "label": {"type": "string"},
                            "url": {"type": "string"},
                        },
                    },
                },
                "required": ["to", "body"],
            },
            min_role=Role.BUILDER,
            run=_send_email,
        ),
        SilaTool(
            # Hidden from the model: dispatched by the pause node after the user
            # confirms in the panel. It refuses unless email_confirmed is set.
            name="confirm_send_email",
            description="Internal: deliver the confirmed email draft.",
            parameters={"type": "object", "properties": {}},
            min_role=Role.BUILDER,
            run=_confirm_send_email,
            extra={"hidden": True},
        ),
        SilaTool(
            name="read_teams_messages",
            description=(
                "Read recent messages from one of the user's Microsoft Teams channels "
                "and attach them to this session (for summaries, documents, or data). "
                "If you don't know the team/channel yet, call it without them — it "
                "lists what the user is in so you can ask."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "refresh": {
                        "type": "boolean",
                        "description": (
                            "Fetch again even if this exact read was done earlier this "
                            "session. Default false: a repeat call returns the text already read."
                        ),
                    },
                    "team": {"type": "string", "description": "Team name (as the user says it)."},
                    "channel": {"type": "string", "description": "Channel name."},
                    "limit": {"type": "integer", "description": "Max messages (default 30, ≤50)."},
                },
            },
            min_role=Role.VIEWER,
            run=_read_teams_messages,
        ),
        SilaTool(
            name="read_meeting_transcript",
            description=(
                "Fetch the transcript of a Microsoft Teams meeting the user organized "
                "and attach it to this session, so you can summarize it, pull decisions "
                "and action items, or draft a follow-up. Call with a Teams join link if "
                "the user pastes one; otherwise call it WITHOUT `meeting` to get their "
                "most recent transcript. The meeting must have had transcription on."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "meeting": {
                        "type": "string",
                        "description": "Optional Teams join URL; omit for the latest transcript.",
                    },
                },
            },
            min_role=Role.VIEWER,
            run=_read_meeting_transcript,
        ),
        SilaTool(
            name="create_docx",
            description=(
                "Write a Word document from a title + sections (heading + body text). "
                "Saves to the user's OneDrive when Word is connected, otherwise returns "
                "a download link. Use for reports, meeting summaries, process documents. "
                "Set also_pdf=true when the user wants a PDF — you get BOTH formats from "
                "this one call, so never make them ask twice."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Document title."},
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string"},
                                "body": {"type": "string"},
                            },
                            "required": ["body"],
                        },
                        "description": "The document's sections, in order.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Plain text body (when sections aren't needed).",
                    },
                    "also_pdf": {
                        "type": "boolean",
                        "description": (
                            "Also return a PDF of the same document. Set true whenever "
                            "the user asks for a PDF — on its own or alongside Word — so "
                            "both arrive from ONE call instead of a second request."
                        ),
                    },
                },
                "required": ["title"],
            },
            min_role=Role.BUILDER,
            run=_create_docx,
        ),
        SilaTool(
            name="track_tasks",
            description=(
                "Publish the task list for a MULTI-STEP request and keep it current. "
                "Call this FIRST whenever the user asks for more than one thing "
                "(e.g. 'search X, show me a table, then build a dashboard'): pass "
                "`tasks` as the steps in order, taken from their words. Then after "
                "EACH step completes, call it again with the same `tasks` and the "
                "finished ones listed in `done`. The user sees the checklist tick "
                "across, and you do not lose the last item of a long request. Do not "
                "use it for a single-step request."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The steps, in order, in the user's own terms.",
                    },
                    "done": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Which of `tasks` are finished — exact same wording.",
                    },
                },
                "required": ["tasks"],
            },
            min_role=Role.VIEWER,
            run=_track_tasks,
        ),
        SilaTool(
            name="create_grafana_dashboard",
            description=(
                "Create a dashboard on the workspace's connected Grafana. If the user "
                "ATTACHED a data file (CSV), just pass a title (+ optional focus "
                "column) — the file's data is embedded so the panels show real "
                "charts. If YOU generated or modelled the data yourself, pass it as "
                "`csv_data` and it is charted exactly the same way. NEVER print a CSV "
                "and ask the user to save it and attach it back — you already have the "
                "data, so send it. Otherwise provide panels: each has a title, a type "
                "(timeseries, stat, gauge, bar, table, piechart, or text) and a "
                "query in the datasource's language (PromQL/SQL). Only use this when "
                "the user asks for a GRAFANA dashboard."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "The dashboard title."},
                    "csv_data": {
                        "type": "string",
                        "description": (
                            "Data YOU produced, as CSV text with a header row. Use this "
                            "whenever the user asked you to model or invent the figures. "
                            "Never ask the user to download and re-upload data you have."
                        ),
                    },
                    "panels": {
                        "type": "array",
                        "description": "The panels to place (two per row).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "timeseries",
                                        "stat",
                                        "gauge",
                                        "bar",
                                        "table",
                                        "piechart",
                                        "text",
                                    ],
                                },
                                "query": {
                                    "type": "string",
                                    "description": "PromQL/SQL/etc. (omit for a text panel).",
                                },
                                "unit": {"type": "string"},
                            },
                            "required": ["title", "type"],
                        },
                    },
                    "datasource_uid": {
                        "type": "string",
                        "description": "Optional Grafana datasource UID for the panels.",
                    },
                    "focus": {
                        "type": "string",
                        "description": "With an attached file: optional column to prioritise.",
                    },
                    "charts": {
                        "type": "array",
                        "description": (
                            "Charts to ADD or CHANGE, described by column names — the "
                            "platform computes the data. Each: {kind: "
                            "line|bar|pie|stat|table, metric: <numeric column>, by: "
                            "<label/category column>, agg: avg|sum|min|max|count, "
                            "title, columns: [<table columns>], limit}. A spec whose "
                            "title matches an existing chart REPLACES it."
                        ),
                        "items": {"type": "object"},
                    },
                    "remove_charts": {
                        "type": "array",
                        "description": "EXACT visible titles of whole cards to remove.",
                        "items": {"type": "string"},
                    },
                },
                "required": ["title"],
            },
            min_role=Role.BUILDER,
            run=_create_grafana_dashboard,
        ),
        SilaTool(
            name="create_slide_deck",
            description=(
                "Build a designed PowerPoint deck (native charts, tables, icons, and "
                "infographic layouts) and save it to the user's OneDrive. Two ways to "
                'pass content: (1) SIMPLE — `slides`: [{"title": str, "bullets": '
                "[str,…]}, …] (first slide is the cover); or (2) RICH — a `deck` object "
                "for charts/tables/process-flows. Pick a `palette` (modern-slate | "
                "minimal-editorial | high-contrast-bold). Pass `template` only when the "
                "user names a workspace/corporate template (its brand colors+fonts "
                "override the palette). Use whenever the user asks for slides / a deck."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Deck title (cover slide)."},
                    "palette": {
                        "type": "string",
                        "enum": [
                            "modern-slate",
                            "minimal-editorial",
                            "high-contrast-bold",
                            "midnight-teal",
                            "warm-sand",
                            "royal-indigo",
                        ],
                        "description": (
                            "Visual palette; defaults to modern-slate. Dark: modern-slate "
                            "(navy/blue), high-contrast-bold (black/lime), midnight-teal "
                            "(teal/aqua), royal-indigo (indigo/gold). Light: minimal-editorial "
                            "(cream/olive), warm-sand (sand/terracotta)."
                        ),
                    },
                    "slides": {
                        "type": "array",
                        "description": "SIMPLE outline: one object per slide (cover first).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "bullets": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["title"],
                        },
                    },
                    "cover_image": {
                        "type": "string",
                        "description": (
                            "Optional: a DECORATIVE scene for a generated hero image on the "
                            "COVER slide (e.g. 'Muscat skyline at sunrise'). Use this with the "
                            "SIMPLE `slides` outline — do NOT hand-build a rich `deck` just for "
                            "a cover picture. Only set when the user asks for a cover image/"
                            "photo; describe scenery only, never text/data. Slow (GPU)."
                        ),
                    },
                    "deck": {
                        "type": "object",
                        "description": (
                            "RICH deck (overrides `slides`). Shape: {slides: [{layoutType, "
                            "slideTitle, subtitle?, containers: [...]}]}. layoutType ∈ "
                            "title-hero | split-grid | 3-column-cards | metric-showcase | "
                            "quote-focus | data-showcase | process-flow | comparison | "
                            "timeline | image-feature. A container is {type: card|metric|"
                            "hero-text|table|chart|image, heading?, body?, accentValue?, "
                            "icon?, rows?, chart?, imagePrompt?}. image-feature holds ONE "
                            "'image' container (imagePrompt = a DECORATIVE scene, never text "
                            "or data — it's slow, GPU-bound, opt-in) + an optional caption. "
                            "icon ∈ check,bolt,gear,database,document,star,arrow,shield,"
                            "chart,plus,cloud,idea,target,people,lock,diamond,hexagon. "
                            "table: rows[][] (first row = header). chart: {kind: bar|line|"
                            "pie, categories:[str], series:[{name,values:[num]}]} on a "
                            "data-showcase slide. Keep it scannable: ≤8-word titles, ≤4 "
                            "items per slide; the engine reflows/paginates and reports any "
                            "fixes to correct. "
                            "A metric-showcase slide MUST carry the FIGURES — each container "
                            "body is the number alone ('25') and the heading names it "
                            "('Agents registered'). A metrics slide with labels and no "
                            "numbers is an empty slide. "
                            "A review or proposal deck MUST END with an explicit ask, and an ask "
                            "has THREE parts or it is not one: WHAT is being approved, WHO "
                            "approves it (a named forum, committee or role), and BY WHEN (a "
                            "date or 'at this meeting'). Put it in its own slide or block, "
                            "never inside a table. Do not end on a table. "
                            "EVERY content slide carries a one-line `subtitle`: a single "
                            "sentence saying what the reader should TAKE from the slide, not a "
                            "restatement of the title. A heading followed straight by a table "
                            "gives the reader no framing. Good: 'Twenty-five agents exist; "
                            "twelve are usable today.' Bad: 'An overview of adoption.' "
                            "Write each container as ONE complete self-contained item — a "
                            "heading that is a whole phrase and a body of whole sentences. "
                            "Never split one sentence across two containers or two slides."
                        ),
                    },
                    "template": {
                        "type": "string",
                        "description": "Workspace/corporate template name (brand override).",
                    },
                },
                "required": ["title"],
            },
            min_role=Role.BUILDER,
            run=_create_slide_deck,
        ),
        SilaTool(
            name="create_data_dashboard",
            description=(
                "Build a visual dashboard (charts + stats) that renders INLINE in this "
                "chat. Two sources: a CSV the user ATTACHED, or — when YOU gathered or "
                "modelled the figures (e.g. from web_search) — pass them as `csv_data` "
                "and they are charted identically. NEVER print a CSV and ask the user "
                "to save it and attach it back: you already have the data, so send it. "
                "Optionally pass `focus` (a column name they care about) to prioritise "
                "it. "
                "EDITS: to CHANGE an existing chart, send it in `charts` with the "
                "SAME title and the new kind/metric — it replaces the old one. To "
                "REMOVE a chart, stat card, or table, put its visible title in "
                "`remove_charts`. Never re-send unchanged charts."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "description": "Optional column to prioritise (user's words).",
                    },
                    "csv_data": {
                        "type": "string",
                        "description": (
                            "Data YOU produced, as CSV text with a header row. Use this "
                            "whenever you researched or modelled the figures yourself. "
                            "Never ask the user to download and re-upload data you have."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Dashboard title. Only used with `csv_data` — it names the "
                            "generated dataset."
                        ),
                    },
                    "file": {
                        "type": "string",
                        "description": "Optional file name when several are attached.",
                    },
                    "charts": {
                        "type": "array",
                        "description": (
                            "Charts to ADD or CHANGE, described by column names — the "
                            "platform computes the data. Each: {kind: "
                            "line|bar|pie|stat|table, metric: <numeric column>, by: "
                            "<label/category column>, agg: avg|sum|min|max|count, "
                            "title, columns: [<table columns>], limit}. A spec whose "
                            "title matches an existing chart REPLACES it (that's how "
                            "you change a chart's kind). pie with a metric = share of "
                            "that metric by label; table = top rows sorted by the "
                            "metric (use `columns` to control which columns show)."
                        ),
                        "items": {"type": "object"},
                    },
                    "remove_charts": {
                        "type": "array",
                        "description": (
                            "EXACT visible titles of WHOLE cards to remove (e.g. "
                            "['Class (avg)', 'EPS Over Time']). To drop a table "
                            "COLUMN, re-send the table in `charts` with reduced "
                            "`columns` instead — never remove the table itself."
                        ),
                        "items": {"type": "string"},
                    },
                },
            },
            min_role=Role.VIEWER,
            run=_create_data_dashboard,
        ),
        SilaTool(
            name="create_powerbi_report",
            description=(
                "Push a data file the user ATTACHED (CSV) into their own Power BI as "
                "a push dataset (My workspace) and return the link where one click "
                "auto-creates a report. ONLY use this when the user explicitly asks "
                "for Power BI. Requires their Power BI connection (Connectors page)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Dataset name (defaults to the file name).",
                    },
                    "file": {
                        "type": "string",
                        "description": "Optional file name when several are attached.",
                    },
                    "charts": {
                        "type": "array",
                        "description": (
                            "Charts to ADD or CHANGE, described by column names — same "
                            "shape as create_data_dashboard's charts ({kind: "
                            "line|bar|pie|stat|table, metric, by, title, columns, "
                            "limit}). Same title = replaces that chart; earlier charts "
                            "are kept automatically."
                        ),
                        "items": {"type": "object"},
                    },
                    "remove_charts": {
                        "type": "array",
                        "description": "EXACT visible titles of whole cards to remove.",
                        "items": {"type": "string"},
                    },
                },
            },
            min_role=Role.BUILDER,
            run=_create_powerbi_report,
        ),
        SilaTool(
            name="hire_agent",
            description=(
                "Add an EXISTING published agent to the user's workspace. Only call "
                "this with an agent_id returned by search_bazaar in this conversation "
                "— never with an id you composed yourself. If the user wants a NEW "
                "agent built ('build me an agent that…', 'create an agent for…'), this "
                "is the wrong tool: use the agent-building tool instead. If they "
                "described a capability but have not chosen a specific agent, call "
                "search_bazaar first and let them pick."
            ),
            parameters={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
            min_role=Role.HIRER,
            run=_hire_agent,
        ),
    )
}


# Fan-out workers (cutover) get a SCOPED toolset — build tools only. No navigate/
# hire/search, and crucially no pausing elicitation: workers are pause-free (inputs
# are pre-collected single-threaded before fan-out), so they never open a modal.
WORKER_TOOL_NAMES = frozenset({"create_agent_workflow"})


def tool_schemas(
    only: frozenset[str] | None = None,
    extra: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Function-calling schema for the registry, optionally scoped to `only`.
    `extra` appends dynamically-discovered tools (the workspace's MCP tools) so
    SILA can call them alongside its curated set. O(n), n<10 curated + |extra|."""
    schemas = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in TOOLS.values()
        if (only is None or t.name in only) and not t.extra.get("hidden")
    ]
    if extra:
        schemas.extend(extra)
    return schemas


# MCP tools require the same role as the tool playground's invoke endpoint.
_MCP_MIN_ROLE = Role.BUILDER


async def execute_tool(name: str, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Run a registry tool with role gating. Unknown/forbidden → refusal text.
    An `mcp__…` name routes to the workspace's registered MCP server (Builder+)."""
    from sila.services.sila import mcp_tools

    if ctx.build_state.get(CONFIDENTIAL_KEY) and (
        name in LEAVES_THE_BOX or mcp_tools.is_mcp_tool(name)
    ):
        return ToolResult(content=_STAYS_IN_SILA, summary="Kept in Sila (confidential)")
    if mcp_tools.is_mcp_tool(name):
        if ROLE_RANK[ctx.role] < ROLE_RANK[_MCP_MIN_ROLE]:
            return ToolResult(
                content=(
                    f"The user's role ({ctx.role}) doesn't permit external tools "
                    f"(requires {_MCP_MIN_ROLE}). Tell them politely."
                )
            )
        output = await mcp_tools.call_mcp_tool(ctx.db, ctx.workspace_id, name, args)
        return ToolResult(content=output, summary="Used a connected tool")

    tool = TOOLS.get(name)
    if tool is None:
        return ToolResult(content=f"Tool '{name}' does not exist.")
    if ROLE_RANK[ctx.role] < ROLE_RANK[tool.min_role]:
        return ToolResult(
            content=(
                f"The user's role ({ctx.role}) doesn't permit '{name}' "
                f"(requires {tool.min_role}). Tell them politely."
            )
        )
    # SAFETY NET (live bug: a tool raising wedged the whole turn — the browser
    # timer counted forever with no error). A crash or a hang becomes a clean
    # tool-error result; the turn always completes. HTTPException passes through
    # (it's a deliberate auth/permission signal, not a tool bug).
    try:
        return await asyncio.wait_for(tool.run(ctx, args), timeout=_TOOL_TIMEOUT_S)
    except HTTPException:
        raise
    except TimeoutError:
        log.error("sila.tool.timeout", tool=name, timeout_s=_TOOL_TIMEOUT_S)
        # The name and the exception stay in the LOG. They must not appear in the
        # text handed to the model: it relays that text, so "'search_bazaar'
        # failed unexpectedly (TypeError)" reached the user verbatim.
        return ToolResult(
            content=(
                "That step timed out. Tell the user that part didn't finish and offer "
                "to retry. Do NOT name tools, functions, or error types — they are "
                "internal."
            ),
            summary="Timed out",
        )
    except Exception as exc:  # noqa: BLE001 — a tool bug must never hang the turn
        log.error("sila.tool.crashed", tool=name, error=f"{type(exc).__name__}: {exc}")
        return ToolResult(
            content=(
                "That step failed. Tell the user briefly that it didn't work and what "
                "they can try instead. Do NOT name tools, functions, or error types — "
                "they are internal."
            ),
            summary="Failed",
        )
