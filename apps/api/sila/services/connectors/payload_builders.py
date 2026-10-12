"""Connector payload builders (WS-9) — the seam for non-flat-JSON actions.

The manifest's `ConnectorAction` maps ONE resource field + ONE text field into a
flat JSON body (Slack `chat.postMessage` fits). Google's actions don't: Gmail
needs a base64url MIME blob, Sheets needs a `values[[…]]` array with the sheet id
IN THE PATH, Calendar needs a structured event. This module is the registry that
turns the logical `data` the workflow node forwards — the chosen resource id + the
agent's generated text — into the provider's REAL endpoint + body, server-side.

Security: the builder runs inside `execute_proxy`, AFTER the capability check, and
constructs the real Google path from the LOGICAL endpoint the capability is bound
to. A resource id in the path (a spreadsheet, a calendar) is therefore built
server-side from the saved binding data — never trusted from the caller's body.

A builder returns `(real_endpoint, real_body)`. Providers with no registered
builder fall through to the identity default (flat JSON, like Slack).

Complexity: O(1) dict lookup; each builder is O(len(text)).
"""

from __future__ import annotations

import base64
import html
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

# A builder: (data) -> (real_endpoint, real_body). `data` is the flat dict the
# workflow node forwards ({resource fields…, "message": <generated text>}).
Builder = Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]

_TEXT_KEYS = ("message", "text", "body", "summary")


def _text(data: dict[str, Any]) -> str:
    """The agent's generated text, whatever field the node injected it into."""
    for k in _TEXT_KEYS:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _attachment(data: dict[str, Any]) -> tuple[str, bytes] | None:
    """The (filename, bytes) a mail action should attach — injected server-side by
    execute_proxy's Doc→PDF enrichment ("email me the file"), never taken raw
    from the workflow. None when there is nothing to attach."""
    blob_b64 = str(data.get("attachment_b64") or "")
    if not blob_b64:
        return None
    try:
        blob = base64.b64decode(blob_b64)
    except (ValueError, TypeError):
        return None
    name = str(data.get("attachment_name") or "attachment.pdf")
    return (name, blob) if blob else None


def _gmail_send(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Gmail send → a base64url-encoded RFC-2822 message ({"raw": …}). `to` is the
    recipient captured in the build; `subject` optional; the body is the agent text.
    `EmailMessage` handles unicode (Arabic subjects/bodies) correctly."""
    msg = EmailMessage()
    to = str(data.get("to") or "").strip()
    if to:
        msg["To"] = to
    msg["Subject"] = str(data.get("subject") or "Your summary")
    msg.set_content(_text(data) or "(no content)")
    attachment = _attachment(data)
    if attachment:
        name, blob = attachment
        msg.add_attachment(blob, maintype="application", subtype="pdf", filename=name)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return "gmail/v1/users/me/messages/send", {"raw": raw}


def _sheets_append(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Sheets append → the spreadsheet id (saved resource) in the PATH, the text as a
    single-cell row. Range defaults to A1 so Sheets appends after the last row."""
    sheet_id = str(data.get("spreadsheet_id") or data.get("spreadsheet") or "").strip()
    rng = str(data.get("range") or "A1")
    endpoint = (
        f"v4/spreadsheets/{quote(sheet_id, safe='')}/values/{quote(rng, safe='')}:append"
        "?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    return endpoint, {"values": [[_text(data)]]}


def _calendar_insert(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Calendar create → an event on the chosen calendar (id in the PATH). The agent
    text is the description; start/end are provided by the build (ISO 8601) or default
    to a 30-min block the operator adjusts. Title falls back to a generic summary."""
    cal_id = str(data.get("calendar_id") or data.get("calendar") or "primary").strip()
    event: dict[str, Any] = {
        "summary": str(data.get("title") or "New event"),
        "description": _text(data),
    }
    start, end = data.get("start"), data.get("end")
    if isinstance(start, str) and isinstance(end, str):
        event["start"] = {"dateTime": start}
        event["end"] = {"dateTime": end}
    return f"calendar/v3/calendars/{quote(cal_id, safe='')}/events", event


def _chat_post(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Google Chat → post the text to the chosen space (resource name in the PATH).
    The space is captured in the build interview and normalized to `spaces/<id>`;
    a bare id is prefixed defensively here too."""
    space = str(data.get("space") or "").strip().strip("/")
    if not space.startswith("spaces/"):
        space = f"spaces/{space}"
    return f"v1/{quote(space, safe='/')}/messages", {"text": _text(data)}


def _tasks_insert(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Google Tasks → a task on the user's default list: the first line of the
    generated text becomes the title, the rest lands in the notes."""
    lines = (_text(data) or "New task").splitlines()
    body: dict[str, Any] = {"title": (lines[0] or "New task")[:120]}
    notes = "\n".join(lines[1:]).strip()
    if notes:
        body["notes"] = notes
    return "tasks/v1/lists/@default/tasks", body


def _docs_create(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Google Docs write → create a titled document via the Docs API (JSON create is
    the flat, safe half; inserting the body needs a follow-up batchUpdate, deferred).
    Title from the build or the first line of the generated text."""
    title = (
        str(data.get("title") or "").strip()
        or (_text(data).splitlines() or ["New document"])[0][:80]
    )
    return "v1/documents", {"title": title}


def _outlook_send(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Outlook (Graph) send → POST me/sendMail. Plain JSON (no MIME blob like
    Gmail); `to` is the recipient captured in the build interview. Graph responds
    202 with an EMPTY body — the client already treats that as success."""
    # A caller that pre-rendered formal HTML (SILA's send_email) passes it in
    # `html`; every other caller (workflow mail nodes) stays plain text.
    html_body = data.get("html")
    if isinstance(html_body, str) and html_body.strip():
        body_part = {"contentType": "HTML", "content": html_body}
    else:
        body_part = {"contentType": "Text", "content": _text(data) or "(no content)"}
    message: dict[str, Any] = {
        "subject": str(data.get("subject") or "Your summary"),
        "body": body_part,
    }
    to = str(data.get("to") or "").strip()
    if to:
        message["toRecipients"] = [{"emailAddress": {"address": to}}]
    attachments: list[dict[str, Any]] = []
    # Inline images referenced by the HTML as cid:<contentId> (e.g. the Sila logo).
    # isInline + contentId make them render in-body, not as visible attachments.
    for img in data.get("inline_images") or []:
        raw = img.get("bytes")
        if not isinstance(raw, bytes | bytearray):
            continue
        attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": str(img.get("name") or "image.png"),
                "contentType": str(img.get("content_type") or "image/png"),
                "contentBytes": base64.b64encode(bytes(raw)).decode(),
                "isInline": True,
                "contentId": str(img.get("content_id") or "inline"),
            }
        )
    attachment = _attachment(data)
    if attachment:
        name, blob = attachment
        attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentType": "application/pdf",
                "contentBytes": base64.b64encode(blob).decode(),
            }
        )
    if attachments:
        message["attachments"] = attachments
    return "v1.0/me/sendMail", {"message": message, "saveToSentItems": True}


def _outlook_event(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Outlook Calendar (Graph) → POST me/events. Graph REQUIRES start+end; when
    the build didn't provide them (ISO 8601), default to a 30-minute block at the
    next full hour UTC — the operator adjusts it in the calendar."""
    start_raw, end_raw = data.get("start"), data.get("end")
    if isinstance(start_raw, str) and isinstance(end_raw, str):
        start, end = start_raw, end_raw
    else:
        nxt = (datetime.now(UTC) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        start = nxt.strftime("%Y-%m-%dT%H:%M:%S")
        end = (nxt + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    return (
        "v1.0/me/events",
        {
            "subject": str(data.get("title") or "New event"),
            "body": {"contentType": "Text", "content": _text(data)},
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
        },
    )


# (provider, logical endpoint) → builder. The logical endpoint matches the
# manifest ConnectorAction.endpoint (the capability is bound to it).
def _teams_post(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Teams (Graph) → post to a channel. The target rides as "teamId/channelId"
    (parsed from the pasted channel link by normalize_destination)."""
    target = str(data.get("teams_target") or "").strip().strip("/")
    team, _, channel = target.partition("/")
    return (
        f"v1.0/teams/{quote(team, safe='')}/channels/{quote(channel, safe='')}/messages",
        {"body": {"contentType": "text", "content": _text(data)}},
    )


def _todo_create(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Microsoft To Do (Graph) → a task on the DEFAULT list. `list_id` is injected
    by the lookup pre-step (GET me/todo/lists → wellknown defaultList); first line
    of the generated text is the title, the rest lands in the body."""
    list_id = str(data.get("list_id") or "").strip()
    lines = (_text(data) or "New task").splitlines()
    body: dict[str, Any] = {"title": (lines[0] or "New task")[:120]}
    notes = "\n".join(lines[1:]).strip()
    if notes:
        body["body"] = {"content": notes, "contentType": "text"}
    return f"v1.0/me/todo/lists/{quote(list_id, safe='')}/tasks", body


def _onenote_page(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """OneNote (Graph) → a page in the default section. The body is XHTML built
    from the generated text (escaped — never raw HTML from the model); Graph
    wants a RAW request body, carried by the __content__ sentinel."""
    title = html.escape(str(data.get("title") or f"Digest — {date.today().isoformat()}"))
    paragraphs = "".join(
        f"<p>{html.escape(line)}</p>" for line in (_text(data) or "(no content)").splitlines()
    )
    xhtml = (
        "<!DOCTYPE html><html><head><title>" + title + "</title></head>"
        "<body>" + paragraphs + "</body></html>"
    )
    return (
        "v1.0/me/onenote/pages",
        {"__content__": xhtml, "__content_type__": "application/xhtml+xml"},
    )


def _onedrive_upload(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """OneDrive (Graph) → save the generated text as a dated markdown file at the
    drive root (PUT path:/content with a RAW body)."""
    name = str(data.get("filename") or f"digest-{date.today().isoformat()}.md")
    return (
        f"v1.0/me/drive/root:/{quote(name, safe='')}:/content",
        {"__content__": _text(data) or "(no content)", "__content_type__": "text/markdown"},
    )


def _sharepoint_upload(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """SharePoint (Graph) → save to the picked SITE's default document library —
    the same drive PUT as OneDrive, on `/sites/{site_id}/drive`. A pre-built
    deck (`__deck_bytes__`, the template-filling path) ships as .pptx; otherwise
    the generated text is saved as a dated markdown file."""
    site_id = str(data.get("site_id") or "").strip()
    deck = data.get("__deck_bytes__")
    if isinstance(deck, bytes):
        name = str(data.get("filename") or f"deck-{date.today().isoformat()}.pptx")
        body: dict[str, Any] = {
            "__content__": deck,
            "__content_type__": (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
        }
    else:
        name = str(data.get("filename") or f"digest-{date.today().isoformat()}.md")
        body = {
            "__content__": _text(data) or "(no content)",
            "__content_type__": "text/markdown",
        }
    # Graph site ids are "host,siteGuid,webGuid" — keep the commas (real ids),
    # escape everything else so a value can never break out of the path segment.
    return (
        f"v1.0/sites/{quote(site_id, safe=',')}/drive/root:/{quote(name, safe='')}:/content",
        body,
    )


def _clickup_task(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """ClickUp → create a task in the interview-captured list. The first line of
    the generated text names the task; the full text is the markdown body."""
    list_id = str(data.get("clickup_list") or "").strip()
    text = _text(data) or "(no content)"
    first_line = text.splitlines()[0].strip().lstrip("#").strip()
    name = str(data.get("title") or first_line or "Task")[:200]
    return (
        f"api/v2/list/{quote(list_id, safe='')}/task",
        {"name": name, "markdown_description": text},
    )


def _excel_append(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Excel (Graph) → append a row to the workbook's first table. `item_id` is
    injected by the share-link pre-step; the table defaults to Table1 (Excel's
    name for the first inserted table)."""
    item_id = str(data.get("item_id") or "").strip()
    table = str(data.get("table") or "Table1").strip()
    return (
        f"v1.0/me/drive/items/{quote(item_id, safe='')}/workbook/tables/"
        f"{quote(table, safe='')}/rows/add",
        {"values": [[_text(data)]]},
    )


def _word_upload(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Word → Sila BUILDS the .docx (Graph has no Word-authoring API) and PUTs it
    to the OneDrive root. Title from the build or a dated default."""
    from sila.services.connectors.office_files import build_docx

    title = str(data.get("title") or f"Digest — {date.today().isoformat()}")
    name = str(data.get("filename") or f"digest-{date.today().isoformat()}.docx")
    # A pre-built document (create_docx's sectioned output) ships as-is; the
    # digest path still builds a simple docx from the generated text.
    prebuilt = data.get("__docx_bytes__")
    doc = (
        prebuilt
        if isinstance(prebuilt, bytes)
        else build_docx(title, _text(data) or "(no content)")
    )
    return (
        f"v1.0/me/drive/root:/{quote(name, safe='')}:/content",
        {
            "__content__": doc,
            "__content_type__": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        },
    )


def _powerpoint_upload(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """PowerPoint → Sila BUILDS a one-slide .pptx (title + a line per bullet) and
    PUTs it to the OneDrive root."""
    from sila.services.connectors.office_files import build_pptx

    title = str(data.get("title") or f"Digest — {date.today().isoformat()}")
    name = str(data.get("filename") or f"digest-{date.today().isoformat()}.pptx")
    # A pre-built deck (the template-filling path) ships as-is; otherwise the
    # minimal built-in deck is generated from the text.
    deck = data.get("__deck_bytes__")
    return (
        f"v1.0/me/drive/root:/{quote(name, safe='')}:/content",
        {
            "__content__": deck
            if isinstance(deck, bytes)
            else build_pptx(title, _text(data) or "(no content)"),
            "__content_type__": (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
        },
    )


# Lookup PRE-STEPS — the inverse of a follow-up: a GET that must run BEFORE the
# action because the target id isn't knowable at build time (To Do's default
# list; the driveItem behind an Excel share link). The lookup callable derives
# (method, endpoint) FROM the action data; the merge folds the response back in.
# Same server-side trust rules as builders.
PreLookup = Callable[[dict[str, Any]], "tuple[str, str] | None"]
PreMerge = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _todo_lists_lookup(_data: dict[str, Any]) -> tuple[str, str] | None:
    return ("GET", "v1.0/me/todo/lists")


def _todo_default_list(data: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    items = response.get("value") or []
    default = next(
        (i for i in items if isinstance(i, dict) and i.get("wellknownListName") == "defaultList"),
        items[0] if items and isinstance(items[0], dict) else None,
    )
    if default and default.get("id"):
        return {**data, "list_id": str(default["id"])}
    return data


def _excel_share_lookup(data: dict[str, Any]) -> tuple[str, str] | None:
    """A pasted share link → Graph's /shares token (u! + base64url, no padding)."""
    link = str(data.get("excel_link") or "").strip()
    if not link:
        return None
    token = "u!" + base64.urlsafe_b64encode(link.encode()).decode().rstrip("=")
    return ("GET", f"v1.0/shares/{token}/driveItem")


def _excel_item_merge(data: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    item_id = str(response.get("id") or "")
    return {**data, "item_id": item_id} if item_id else data


_PRE_STEPS: dict[tuple[str, str], tuple[PreLookup, PreMerge]] = {
    ("ms-todo", "todo/tasks/create"): (_todo_lists_lookup, _todo_default_list),
    ("ms-excel", "excel/rows/append"): (_excel_share_lookup, _excel_item_merge),
}


def pre_step_for(provider: str, endpoint: str, data: dict[str, Any]) -> tuple[str, str] | None:
    """The lookup (method, endpoint) an action needs before it can run, or None."""
    entry = _PRE_STEPS.get((provider, endpoint))
    return entry[0](data) if entry else None


def apply_pre_step(
    provider: str, endpoint: str, data: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    """Merge the lookup response into the action data. Identity when unregistered."""
    entry = _PRE_STEPS.get((provider, endpoint))
    return entry[1](data, response) if entry else data


_BUILDERS: dict[tuple[str, str], Builder] = {
    ("gmail", "users/messages/send"): _gmail_send,
    ("google-sheets", "values/append"): _sheets_append,
    ("google-calendar", "events/insert"): _calendar_insert,
    ("google-docs", "documents/create"): _docs_create,
    ("google-chat", "spaces/messages/create"): _chat_post,
    ("google-tasks", "tasks/insert"): _tasks_insert,
    ("outlook-mail", "me/sendMail"): _outlook_send,
    ("outlook-calendar", "me/events"): _outlook_event,
    ("ms-teams", "teams/messages/post"): _teams_post,
    ("ms-todo", "todo/tasks/create"): _todo_create,
    ("ms-onenote", "onenote/pages/create"): _onenote_page,
    ("ms-onedrive", "drive/upload"): _onedrive_upload,
    ("ms-excel", "excel/rows/append"): _excel_append,
    ("ms-word", "word/upload"): _word_upload,
    ("ms-powerpoint", "powerpoint/upload"): _powerpoint_upload,
    ("ms-sharepoint", "sharepoint/upload"): _sharepoint_upload,
    ("clickup", "clickup/tasks/create"): _clickup_task,
}


# Composite actions — a logical action that needs a SECOND provider call built
# from the first call's response. Docs is the canonical case: the API has no
# create-with-content, so create returns the documentId and a batchUpdate
# inserts the generated text as the body. A follow-up returns (endpoint, body)
# or None to skip. Same server-side trust rules as the builders.
FollowUp = Callable[[dict[str, Any], dict[str, Any]], "tuple[str, dict[str, Any]] | None"]


def _docs_insert_body(
    data: dict[str, Any], response: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """Docs create -> insert the agent's text as the document body (index 1 =
    right after the title marker). No documentId or no text -> nothing to do."""
    doc_id = str(response.get("documentId") or "")
    text = _text(data)
    if not doc_id or not text:
        return None
    return (
        f"v1/documents/{quote(doc_id, safe='')}:batchUpdate",
        {"requests": [{"insertText": {"location": {"index": 1}, "text": text + "\n"}}]},
    )


_FOLLOW_UPS: dict[tuple[str, str], FollowUp] = {
    ("google-docs", "documents/create"): _docs_insert_body,
}


def follow_up_payload(
    provider: str, endpoint: str, data: dict[str, Any] | None, response: dict[str, Any] | None
) -> tuple[str, dict[str, Any]] | None:
    """The second call of a composite action, or None. O(1) lookup."""
    follow = _FOLLOW_UPS.get((provider, endpoint))
    return follow(data or {}, response or {}) if follow else None


def build_payload(
    provider: str, endpoint: str, data: dict[str, Any] | None
) -> tuple[str, dict[str, Any] | None]:
    """Resolve the real (endpoint, body) for a proxied action. No registered
    builder → identity (the flat JSON the node already assembled, e.g. Slack)."""
    builder = _BUILDERS.get((provider, endpoint))
    if builder is None:
        return endpoint, data
    return builder(data or {})
