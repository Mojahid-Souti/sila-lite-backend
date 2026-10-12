"""Connector READ helpers — pull the user's own data through their own binding.

Until now every registered connector action was a WRITE (post/send/upload).
These helpers add the read direction for the Data-Analyst template and the
Process-Design-Document enablers: Google Sheets values, Outlook mail, and
Teams channel messages.

Security model (mirrors ConnectorService.execute_proxy):
  - the caller's OWN binding only — (user_id, workspace_id, provider) lookup,
    fail-closed when missing (ConnectorError);
  - fixed manifest hosts (Graph / Sheets) — no user-supplied URLs, no SSRF;
  - hard size caps (rows / messages / bytes) so a read can't balloon context;
  - every read audited path-only as `connector.read` (no content in the log).

Missing OAuth SCOPE (a binding created before the read scopes were added)
surfaces as a Nango/Graph 401/403 → callers get a ConnectorError telling the
user to reconnect the provider.

Complexity: each read is ONE proxy call, O(rows) to normalize.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.connectors.nango_client import NangoError
from sila.core.constants import Action, Outcome, ResourceType
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.connectors import ConnectorBindingRepository
from sila.services.connectors.connector_service import ConnectorError, NoMessagesError, make_nango
from sila.services.connectors.manifest import spec_for

MAX_CSV_BYTES = 500_000  # ≤500 KB of normalized CSV per read
MAX_SHEET_ROWS = 2_000
MAX_MESSAGES = 50


class _TextExtractor(HTMLParser):
    """Minimal HTML→text for Graph message bodies (stdlib, no JS, no bs4)."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_html(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — a broken fragment degrades to raw text
        return html
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


async def _binding_for(
    db: AsyncSession, *, user_id: uuid.UUID, workspace_id: uuid.UUID, provider: str
) -> tuple[Any, Any]:
    """(spec, binding) for the caller's own connection — fail-closed."""
    spec = spec_for(provider)
    if spec is None or spec.auth_type != "oauth":
        raise ConnectorError(f"'{provider}' is not a readable connector")
    binding = await ConnectorBindingRepository(db).get(
        user_id=user_id, workspace_id=workspace_id, provider=spec.provider
    )
    if binding is None:
        raise ConnectorError(f"{spec.label} is not connected")
    return spec, binding


async def _audit_read(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    provider: str,
    endpoint: str,
    rows: int,
) -> None:
    # Path-only, never content — same discipline as connector.proxy audits.
    await AuditRepository(db).record(
        actor_user_id=user_id,
        actor_workspace_id=workspace_id,
        action=Action.CONNECTOR_READ,
        resource_type=ResourceType.CONNECTOR_BINDING,
        resource_id=workspace_id,
        outcome=Outcome.SUCCESS,
        metadata={"provider": provider, "endpoint": endpoint.split("?")[0], "rows": rows},
    )


async def _proxy_get(
    spec: Any, binding: Any, endpoint: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    nango = make_nango()
    try:
        return await nango.proxy(
            connection_id=binding.nango_connection_id,
            provider_config_key=spec.nango_integration_id,
            method="GET",
            endpoint=endpoint,
            params=params,
            base_url_override=spec.api_base_url,
        )
    except NangoError as err:
        text = str(err)
        if "401" in text or "403" in text:
            raise ConnectorError(
                f"{spec.label} refused the read — the connection predates read access. "
                f"Reconnect {spec.label} on the Connectors page to grant it."
            ) from err
        raise ConnectorError(f"{spec.label} read failed: {err}") from err


async def _proxy_get_raw(
    spec: Any, binding: Any, endpoint: str, params: dict[str, Any] | None = None
) -> bytes:
    """A GET whose response is NOT JSON (e.g. a WebVTT transcript). Same auth +
    scope-error handling as _proxy_get; returns the raw body."""
    nango = make_nango()
    try:
        return await nango.proxy_raw(
            connection_id=binding.nango_connection_id,
            provider_config_key=spec.nango_integration_id,
            method="GET",
            endpoint=endpoint,
            params=params,
            base_url_override=spec.api_base_url,
        )
    except NangoError as err:
        text = str(err)
        if "401" in text or "403" in text:
            raise ConnectorError(
                f"{spec.label} refused the read — the connection predates read access. "
                f"Reconnect {spec.label} on the Connectors page to grant it."
            ) from err
        raise ConnectorError(f"{spec.label} read failed: {err}") from err


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

_SHEET_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)")


def sheet_id_from(ref: str) -> str:
    """A pasted Sheets URL → the bare spreadsheet id (or the ref unchanged)."""
    m = _SHEET_ID_RE.search(ref)
    return m.group(1) if m else ref.strip()


async def read_google_sheet(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    ref: str,
    cell_range: str | None = None,
) -> tuple[str, str]:
    """Sheet values → (filename, csv_text). One GET; O(cells) to normalize."""
    spec, binding = await _binding_for(
        db, user_id=user_id, workspace_id=workspace_id, provider="google-sheets"
    )
    sid = sheet_id_from(ref)
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", sid):
        raise ConnectorError("That doesn't look like a Google Sheet link or id")
    rng = (cell_range or f"A1:ZZ{MAX_SHEET_ROWS}").strip()
    if not re.fullmatch(r"[A-Za-z0-9!:$ '._-]{1,80}", rng):
        raise ConnectorError("That range doesn't look valid")
    endpoint = f"v4/spreadsheets/{sid}/values/{rng}"
    body = await _proxy_get(spec, binding, endpoint, {"majorDimension": "ROWS"})
    values = body.get("values") or []
    if not values:
        raise ConnectorError("The sheet came back empty — is the range right?")
    buf = io.StringIO()
    writer = csv.writer(buf)
    width = max(len(r) for r in values)
    for row in values[:MAX_SHEET_ROWS]:
        writer.writerow([str(c) for c in list(row) + [""] * (width - len(row))])
        if buf.tell() > MAX_CSV_BYTES:
            break
    await _audit_read(
        db,
        user_id=user_id,
        workspace_id=workspace_id,
        provider="google-sheets",
        endpoint=endpoint,
        rows=len(values),
    )
    return f"sheet-{sid[:8]}.csv", buf.getvalue()[:MAX_CSV_BYTES]


# ---------------------------------------------------------------------------
# Outlook mail
# ---------------------------------------------------------------------------


async def read_outlook_emails(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    folder: str = "inbox",
    search: str | None = None,
    since_days: int = 7,
    limit: int = 25,
    on_item: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> tuple[str, str, str, str]:
    """Recent mail → (csv_name, csv_text, txt_name, txt_text).

    CSV (from, subject, received, preview) is chartable; TXT carries the
    HTML-stripped bodies for summaries/documents. One GET; O(messages).

    `on_item(done, total, {title, meta})` fires per message as it is processed,
    so the caller can show a counted, scrolling read instead of a bare label.
    The loop yields to the event loop between items so those frames actually
    flush — the Graph call returns everything at once, and without the yield
    the whole list would land as one burst after the fact.
    """
    spec, binding = await _binding_for(
        db, user_id=user_id, workspace_id=workspace_id, provider="outlook-mail"
    )
    folder_safe = re.sub(r"[^A-Za-z0-9]", "", folder) or "inbox"
    limit = max(1, min(int(limit), MAX_MESSAGES))
    params: dict[str, Any] = {
        "$top": limit,
        "$select": "from,subject,receivedDateTime,bodyPreview,body",
    }
    if search:
        # Graph: $search can't combine with $orderby/$filter — recency is implicit.
        params["$search"] = f'"{search[:100]}"'
    else:
        params["$orderby"] = "receivedDateTime desc"
        since = (datetime.now(UTC) - timedelta(days=max(1, since_days))).isoformat()
        params["$filter"] = f"receivedDateTime ge {since}"
    endpoint = f"v1.0/me/mailFolders/{folder_safe}/messages"
    body = await _proxy_get(spec, binding, endpoint, params)
    messages = body.get("value") or []
    if not messages:
        raise NoMessagesError("No emails matched — try a wider window or another folder.")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["from", "subject", "received", "preview"])
    texts: list[str] = []
    total = len(messages)
    for index, m in enumerate(messages, start=1):
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
        subject = str(m.get("subject") or "")
        received = str(m.get("receivedDateTime") or "")
        writer.writerow([sender, subject, received, str(m.get("bodyPreview") or "")])
        full = _strip_html(str((m.get("body") or {}).get("content") or ""))[:4_000]
        texts.append(f"From: {sender}\nSubject: {subject}\nReceived: {received}\n{full}\n---")
        if on_item is not None:
            on_item(index, total, {"title": subject or "(no subject)", "meta": sender})
            await asyncio.sleep(0)
    await _audit_read(
        db,
        user_id=user_id,
        workspace_id=workspace_id,
        provider="outlook-mail",
        endpoint=endpoint,
        rows=len(messages),
    )
    return (
        "outlook-emails.csv",
        buf.getvalue()[:MAX_CSV_BYTES],
        "outlook-emails.txt",
        "\n".join(texts)[:MAX_CSV_BYTES],
    )


async def read_email_attachments(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    subject: str | None = None,
    folder: str = "inbox",
    max_files: int = 6,
    max_bytes: int = 4_000_000,
) -> tuple[str, list[tuple[str, str, bytes]]]:
    """Pull the file attachments off a recent email → (matched_subject,
    [(name, content_type, bytes)]). Picks the most recent message that HAS
    attachments; a `subject` hint narrows to the message whose subject
    contains it. Own-binding only; oversize attachments are skipped, not
    truncated. O(attachments)."""
    spec, binding = await _binding_for(
        db, user_id=user_id, workspace_id=workspace_id, provider="outlook-mail"
    )
    folder_safe = re.sub(r"[^A-Za-z0-9]", "", folder) or "inbox"
    # `$expand=attachments` pulls the bytes inline with the message list. We do
    # NOT fetch /messages/{id}/attachments in a second call: Graph message ids
    # contain '+/=' which Nango's proxy will not carry in a URL path (400). Only
    # messages that actually have attachments carry any bytes, so the payload
    # stays small for a normal inbox. O(messages).
    params = {
        "$top": 15,
        "$select": "id,subject,receivedDateTime,hasAttachments",
        "$orderby": "receivedDateTime desc",
        "$expand": "attachments",
    }
    body = await _proxy_get(spec, binding, f"v1.0/me/mailFolders/{folder_safe}/messages", params)
    messages = [m for m in (body.get("value") or []) if m.get("hasAttachments")]
    if subject:
        needle = subject.lower()
        matched = [m for m in messages if needle in str(m.get("subject") or "").lower()]
        messages = matched or messages
    if not messages:
        raise ConnectorError("No email with attachments matched — check the subject or folder.")
    msg = messages[0]
    out: list[tuple[str, str, bytes]] = []
    for a in msg.get("attachments") or []:
        if a.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue  # skip item/reference attachments — only real files
        content_b64 = a.get("contentBytes")
        if not content_b64:
            continue
        try:
            raw = base64.b64decode(content_b64)
        except (ValueError, TypeError):
            continue
        if len(raw) > max_bytes:
            continue  # too big — skip rather than truncate a binary file
        out.append((str(a.get("name") or "attachment"), str(a.get("contentType") or ""), raw))
        if len(out) >= max_files:
            break
    await _audit_read(
        db,
        user_id=user_id,
        workspace_id=workspace_id,
        provider="outlook-mail",
        endpoint="me/messages/attachments",
        rows=len(out),
    )
    return str(msg.get("subject") or ""), out


# ---------------------------------------------------------------------------
# Microsoft Teams
# ---------------------------------------------------------------------------


async def list_teams_and_channels(
    db: AsyncSession, *, user_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Joined teams with their channels: [{id, name, channels: [{id, name}]}].
    O(teams) proxy calls — teams per user is small and this only runs when the
    user hasn't named a channel yet."""
    spec, binding = await _binding_for(
        db, user_id=user_id, workspace_id=workspace_id, provider="ms-teams"
    )
    teams_body = await _proxy_get(spec, binding, "v1.0/me/joinedTeams", {"$top": 20})
    out: list[dict[str, Any]] = []
    for team in (teams_body.get("value") or [])[:10]:
        tid = str(team.get("id") or "")
        chans = await _proxy_get(spec, binding, f"v1.0/teams/{tid}/channels")
        out.append(
            {
                "id": tid,
                "name": str(team.get("displayName") or ""),
                "channels": [
                    {"id": str(c.get("id") or ""), "name": str(c.get("displayName") or "")}
                    for c in (chans.get("value") or [])
                ],
            }
        )
    return out


async def read_teams_messages(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    team_id: str,
    channel_id: str,
    limit: int = 30,
    on_item: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> tuple[str, str]:
    """Channel messages → (txt_name, text). One GET; O(messages).

    `on_item` as in read_outlook_emails: a counted per-message callback.
    """
    spec, binding = await _binding_for(
        db, user_id=user_id, workspace_id=workspace_id, provider="ms-teams"
    )
    if not re.fullmatch(r"[A-Za-z0-9:@._-]{5,}", team_id) or not re.fullmatch(
        r"[A-Za-z0-9:@._-]{5,}", channel_id
    ):
        raise ConnectorError("That team/channel id doesn't look valid")
    limit = max(1, min(int(limit), MAX_MESSAGES))
    endpoint = f"v1.0/teams/{team_id}/channels/{channel_id}/messages"
    body = await _proxy_get(spec, binding, endpoint, {"$top": limit})
    messages = body.get("value") or []
    if not messages:
        raise ConnectorError("That channel has no recent messages.")
    lines: list[str] = []
    total = len(messages)
    for index, m in enumerate(messages, start=1):
        author = ((m.get("from") or {}).get("user") or {}).get("displayName", "")
        when = str(m.get("createdDateTime") or "")
        text = _strip_html(str((m.get("body") or {}).get("content") or ""))[:2_000]
        if text:
            lines.append(f"[{when}] {author}: {text}")
        if on_item is not None:
            on_item(index, total, {"title": (text or "(no text)")[:90], "meta": author})
            await asyncio.sleep(0)
    await _audit_read(
        db,
        user_id=user_id,
        workspace_id=workspace_id,
        provider="ms-teams",
        endpoint=endpoint,
        rows=len(messages),
    )
    return "teams-messages.txt", "\n".join(reversed(lines))[:MAX_CSV_BYTES]


# ---------------------------------------------------------------------------
# Microsoft Teams — online-meeting transcripts (Graph)
# ---------------------------------------------------------------------------

MAX_TRANSCRIPT_CHARS = 60_000  # a long meeting still fits the model context

# A WebVTT speaker cue: <v Speaker Name>spoken text</v>.
_VTT_SPEAKER_RE = re.compile(r"<v\s+([^>]+)>(.*?)</v>", re.DOTALL)
# A Teams meeting join URL — the id/thread is opaque, so we filter Graph by it.
_JOIN_URL_RE = re.compile(r"https://teams\.microsoft\.com/l/meetup-join/[^\s'\"]+")


def _vtt_to_text(vtt: str) -> str:
    """WebVTT transcript → readable "Speaker: line" text. Consecutive lines from
    the same speaker are merged. Falls back to timestamp-stripped text if the VTT
    has no speaker cues. O(cues)."""
    cues = _VTT_SPEAKER_RE.findall(vtt)
    if cues:
        lines: list[str] = []
        last_speaker = ""
        for speaker, spoken in cues:
            name = speaker.strip()
            text = re.sub(r"\s+", " ", _strip_html(spoken)).strip()
            if not text:
                continue
            if name == last_speaker and lines:
                lines[-1] += f" {text}"
            else:
                lines.append(f"{name}: {text}")
                last_speaker = name
        return "\n".join(lines)
    # No speaker tags — drop WEBVTT header, cue numbers and timestamps.
    kept = [
        ln.strip()
        for ln in vtt.splitlines()
        if ln.strip()
        and "-->" not in ln
        and ln.strip().upper() != "WEBVTT"
        and not ln.strip().isdigit()
    ]
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


async def read_meeting_transcript(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    meeting: str | None = None,
    max_chars: int = MAX_TRANSCRIPT_CHARS,
) -> tuple[str, str]:
    """Fetch a Teams online-meeting transcript for the caller → (subject, text).

    `meeting` is either a Teams join URL (resolved via $filter=JoinWebUrl) or
    empty to take the caller's MOST RECENT transcript across their meetings
    (beta getAllTranscripts). Own-binding only; the meeting must have had
    transcription turned on. O(transcripts) + one raw content fetch."""
    spec, binding = await _binding_for(
        db, user_id=user_id, workspace_id=workspace_id, provider="ms-teams"
    )
    hint = (meeting or "").strip()
    subject = "Meeting"
    meeting_id: str | None = None
    transcripts: list[dict[str, Any]] = []

    join_match = _JOIN_URL_RE.search(hint)
    if join_match:
        # Resolve the specific meeting from its join URL, then list its transcripts.
        found = await _proxy_get(
            spec,
            binding,
            "v1.0/me/onlineMeetings",
            {"$filter": f"JoinWebUrl eq '{join_match.group(0)}'"},
        )
        items = found.get("value") or []
        if not items:
            raise ConnectorError(
                "Couldn't find that meeting from the link — it must be a Teams meeting "
                "you organized on this account."
            )
        meeting_id = str(items[0].get("id") or "")
        subject = str(items[0].get("subject") or "Meeting")
        got = await _proxy_get(spec, binding, f"v1.0/me/onlineMeetings/{meeting_id}/transcripts")
        transcripts = got.get("value") or []
    else:
        # No link → the caller's recent transcripts across meetings (beta method).
        got = await _proxy_get(
            spec, binding, "beta/me/onlineMeetings/getAllTranscripts", {"$top": 20}
        )
        transcripts = got.get("value") or []

    if not transcripts:
        raise ConnectorError(
            "No transcript found. During the meeting choose More → Record and "
            "transcribe → Start transcription, end the meeting, then ask again."
        )
    # Newest first.
    transcripts.sort(key=lambda t: str(t.get("createdDateTime") or ""), reverse=True)
    latest = transcripts[0]
    transcript_id = str(latest.get("id") or "")
    mid = meeting_id or str(latest.get("meetingId") or "")
    if not mid or not transcript_id:
        raise ConnectorError("The transcript is missing its meeting reference — can't fetch it.")

    raw = await _proxy_get_raw(
        spec,
        binding,
        f"v1.0/me/onlineMeetings/{mid}/transcripts/{transcript_id}/content",
        {"$format": "text/vtt"},
    )
    text = _vtt_to_text(raw.decode("utf-8", "replace"))[:max_chars]
    if not text:
        raise ConnectorError("The transcript came back empty.")
    await _audit_read(
        db,
        user_id=user_id,
        workspace_id=workspace_id,
        provider="ms-teams",
        endpoint="me/onlineMeetings/transcripts/content",
        rows=text.count("\n") + 1,
    )
    return subject, text
