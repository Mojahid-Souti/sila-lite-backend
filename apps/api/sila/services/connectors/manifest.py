"""Connector manifest (WS-9) — declarative, one entry per connector.

Adding a connector is a manifest entry here + a Nango integration config in the
Nango dashboard (client_id/secret, scopes). **No bespoke OAuth code per
provider** — if a new provider needs Python code, the manifest design is wrong.

`resource_pickers` describe the post-auth selection a user makes (e.g. Slack →
pick a channel). Each picker names the provider list API to call *through Nango's
proxy* and how to read the items out of the response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ResourcePicker:
    """A post-auth choice populated by a proxied list call.

    Slack channel → list_endpoint="conversations.list", items_path="channels",
    value_key="id", label_key="name".
    """

    id: str  # stable key stored in the binding, e.g. "channel"
    label: str  # human label for the picker, e.g. "Channel"
    list_endpoint: str  # provider path called via the Nango proxy
    list_method: str = "GET"
    items_path: str = ""  # dot-path into the proxied response to the list
    value_key: str = "id"  # field on each item used as the stored value
    label_key: str = "name"  # field on each item shown to the user
    params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorAction:
    """The single agent action this connector performs (declarative).

    Generic for connectors whose action body is flat JSON: a saved resource
    (e.g. the chosen Slack channel) maps into one payload field, and the agent's
    generated text maps into another. Slack `chat.postMessage` fits; so would
    most "post/create" REST endpoints — a manifest entry, no code.

    NOT expressive enough for exotic payloads (e.g. Gmail send needs a base64
    MIME blob). Those need a registered payload builder — call that out instead
    of pretending the mapping covers it.
    """

    endpoint: str  # e.g. "chat.postMessage"
    method: str = "POST"
    label: str = "Run action"
    # The saved resource (picker id) → the payload field it fills.
    resource_arg: str | None = None  # picker id, e.g. "channel"
    resource_field: str | None = None  # payload field, e.g. "channel"
    # The payload field the agent's generated text fills at runtime (n8n expr).
    message_field: str | None = None  # e.g. "text"
    # Constant payload fields merged in.
    static: tuple[tuple[str, str], ...] = ()
    # Connectors WITHOUT a resource picker (the Google least-privilege pattern)
    # capture their target in the BUILD INTERVIEW instead: the build_slots slot
    # name → the payload field the captured value fills (what the payload
    # builder reads, e.g. gmail_to → "to").
    slot_arg: str | None = None  # interview slot, e.g. "gmail_to"
    slot_field: str | None = None  # payload field, e.g. "to"


@dataclass(frozen=True)
class ConnectorSpec:
    provider: str
    label: str
    nango_integration_id: str  # the Provider-Config-Key configured in Nango
    auth_type: str  # "oauth" (Nango Connect) | "api_key" (kept on the B2 Incognito path)
    # Proxy base-URL override. Our Google integrations are registered under the
    # GENERIC `google` Nango template (base www.googleapis.com) — fine for the
    # legacy shared-host products (gmail, calendar, tasks) but a 404 for the
    # per-product hosts (docs/sheets/chat.googleapis.com). Sent as Nango's
    # Base-Url-Override header; None = use the template's base.
    api_base_url: str | None = None
    scopes: tuple[str, ...] = ()  # least-privilege; informational (real scopes set in Nango)
    resource_pickers: tuple[ResourcePicker, ...] = ()
    # The one action an agent can perform with this connector. None = connect +
    # pick only (no agent action wired yet). Declarative → adding a flat-JSON
    # action is a manifest entry, not node-builder code.
    action: ConnectorAction | None = None


# The catalog. Slack first; Google is added in Slice 7 as a manifest entry only.
CONNECTORS: dict[str, ConnectorSpec] = {
    "slack": ConnectorSpec(
        provider="slack",
        label="Slack",
        nango_integration_id="slack",
        auth_type="oauth",
        # channels:join lets a freshly-connected bot self-join a public channel before
        # posting (no manual invite). Required — a fresh Nango re-register MUST include it.
        scopes=("chat:write", "channels:read", "channels:join"),
        resource_pickers=(
            ResourcePicker(
                id="channel",
                label="Channel",
                list_endpoint="conversations.list",
                list_method="GET",
                items_path="channels",
                value_key="id",
                label_key="name",
                params={"types": "public_channel", "limit": "200"},
            ),
        ),
        action=ConnectorAction(
            endpoint="chat.postMessage",
            label="Post a message",
            resource_arg="channel",
            resource_field="channel",
            message_field="text",
        ),
    ),
    # --- Google, per-product least-privilege (WS-9) ---------------------------
    # Each is its OWN Nango integration + OAuth scope set, so an agent that sends
    # mail can't touch Drive. The non-flat payloads (base64 MIME, values arrays,
    # event structs) are handled by payload_builders, keyed on the logical
    # ConnectorAction.endpoint below — the manifest stays declarative.
    # Google write-actions take NO resource picker on purpose: requiring a
    # Drive-list scope just to pick a sheet/calendar would bloat the scope. The
    # target (recipient, spreadsheet id, calendar) is captured in the build
    # interview instead — tighter least-privilege, one scope per connector.
    "gmail": ConnectorSpec(
        provider="gmail",
        label="Gmail",
        nango_integration_id="gmail",
        auth_type="oauth",
        scopes=("https://www.googleapis.com/auth/gmail.send",),
        action=ConnectorAction(
            endpoint="users/messages/send",  # logical → payload_builders builds the real path
            label="Send an email",
            message_field="message",
            slot_arg="gmail_to",
            slot_field="to",
        ),
    ),
    "google-sheets": ConnectorSpec(
        provider="google-sheets",
        label="Google Sheets",
        nango_integration_id="google-sheets",
        auth_type="oauth",
        api_base_url="https://sheets.googleapis.com",
        scopes=("https://www.googleapis.com/auth/spreadsheets",),
        action=ConnectorAction(
            endpoint="values/append",  # logical → _sheets_append embeds the sheet id in the path
            label="Append a row",
            message_field="message",
            slot_arg="sheet_id",
            slot_field="spreadsheet_id",
        ),
    ),
    "google-calendar": ConnectorSpec(
        provider="google-calendar",
        label="Google Calendar",
        nango_integration_id="google-calendar",
        auth_type="oauth",
        scopes=("https://www.googleapis.com/auth/calendar.events",),
        action=ConnectorAction(
            endpoint="events/insert",  # logical → _calendar_insert builds the event
            label="Create an event",
            message_field="message",
        ),
    ),
    "google-docs": ConnectorSpec(
        provider="google-docs",
        label="Google Docs",
        nango_integration_id="google-docs",
        auth_type="oauth",
        api_base_url="https://docs.googleapis.com",
        # drive.file = the app can only touch documents IT creates (per-file), the
        # tightest write scope — never the user's whole Drive.
        scopes=("https://www.googleapis.com/auth/drive.file",),
        action=ConnectorAction(
            endpoint="documents/create",  # logical → _docs_create makes a titled doc
            label="Create a document",
            message_field="message",
        ),
    ),
    # --- Tier-1 remainder (roadmap 2026-07-03). Connect + consent work for all
    # five today; agent actions are wired only where they fit the JSON publisher
    # seam. action=None entries state why inline.
    "google-drive": ConnectorSpec(
        provider="google-drive",
        label="Google Drive",
        nango_integration_id="google-drive",
        auth_type="oauth",
        scopes=("https://www.googleapis.com/auth/drive.file",),
        # No agent action yet: a file upload needs a multipart body, which the
        # JSON-only Nango proxy seam can't carry. Transport decision pending
        # (CONNECTOR_ROADMAP) — until then Drive is connect-only.
    ),
    "google-chat": ConnectorSpec(
        provider="google-chat",
        label="Google Chat",
        nango_integration_id="google-chat",
        auth_type="oauth",
        api_base_url="https://chat.googleapis.com",
        scopes=("https://www.googleapis.com/auth/chat.messages.create",),
        action=ConnectorAction(
            endpoint="spaces/messages/create",  # logical → _chat_post embeds the space
            label="Post to a space",
            message_field="message",
            # No spaces-list picker (would need a read scope) — the space is
            # captured in the build interview, like the Gmail recipient.
            slot_arg="chat_space",
            slot_field="space",
        ),
    ),
    "google-contacts": ConnectorSpec(
        provider="google-contacts",
        label="Google Contacts",
        nango_integration_id="google-contacts",
        auth_type="oauth",
        scopes=("https://www.googleapis.com/auth/contacts",),
        # No agent action yet: creating a contact from generated digest text makes
        # no sense in the publisher seam, and lookup is READ-class (trigger design
        # pending). Connect-only until a real action shape exists.
    ),
    "google-tasks": ConnectorSpec(
        provider="google-tasks",
        label="Google Tasks",
        nango_integration_id="google-tasks",
        auth_type="oauth",
        scopes=("https://www.googleapis.com/auth/tasks",),
        action=ConnectorAction(
            endpoint="tasks/insert",  # logical → _tasks_insert targets the default list
            label="Create a task",
            message_field="message",
        ),
    ),
    "google-forms": ConnectorSpec(
        provider="google-forms",
        label="Google Forms",
        nango_integration_id="google-forms",
        auth_type="oauth",
        scopes=("https://www.googleapis.com/auth/forms.responses.readonly",),
        # READ class (responses) — needs the trigger/polling capability design
        # before any agent action. Connect-only for now.
    ),
    # --- Microsoft (Graph) — Tier M1 ------------------------------------------
    # One Azure app, one API, plain JSON everywhere. Registered in Nango under
    # the generic `microsoft` template, so the Graph host rides Base-Url-Override
    # explicitly (the Google generic-template 404 lesson, applied proactively).
    "outlook-mail": ConnectorSpec(
        provider="outlook-mail",
        label="Outlook",
        nango_integration_id="outlook-mail",
        auth_type="oauth",
        api_base_url="https://graph.microsoft.com",
        # Mail.Read powers the read_outlook_emails tool (Data-Analyst / PDD
        # templates). Mirror in the Nango integration; connections made before
        # this scope must reconnect to grant it.
        scopes=("Mail.Send", "Mail.Read"),
        action=ConnectorAction(
            endpoint="me/sendMail",  # logical → _outlook_send builds the Graph payload
            label="Send an email",
            message_field="message",
            slot_arg="outlook_to",
            slot_field="to",
        ),
    ),
    "outlook-calendar": ConnectorSpec(
        provider="outlook-calendar",
        label="Outlook Calendar",
        nango_integration_id="outlook-calendar",
        auth_type="oauth",
        api_base_url="https://graph.microsoft.com",
        scopes=("Calendars.ReadWrite",),
        action=ConnectorAction(
            endpoint="me/events",  # logical → _outlook_event builds the Graph event
            label="Create an event",
            message_field="message",
        ),
    ),
    # Tier M2 — Teams needs a WORK/SCHOOL account (consumer accounts have no
    # Graph channel APIs); the channel is captured as a pasted channel link.
    "ms-teams": ConnectorSpec(
        provider="ms-teams",
        label="Microsoft Teams",
        nango_integration_id="ms-teams",
        auth_type="oauth",
        api_base_url="https://graph.microsoft.com",
        # Read scopes power read_teams_messages (channel discovery + history) and
        # read_meeting_transcript (online-meeting transcripts). Mirror in the Nango
        # integration; older connections must reconnect to gain the new scopes.
        scopes=(
            "ChannelMessage.Send",
            "ChannelMessage.Read.All",
            "Team.ReadBasic.All",
            # Team.ReadBasic.All lists the teams you are IN; it does NOT let you
            # list a team's channels. GET /teams/{id}/channels needs this one,
            # and without it channel discovery 403s — so an agent build could
            # never offer a channel to post to. Graph named the missing scope in
            # the error body the whole time; we were discarding it.
            "Channel.ReadBasic.All",
            "OnlineMeetings.Read",
            "OnlineMeetingTranscript.Read.All",
        ),
        action=ConnectorAction(
            endpoint="teams/messages/post",  # logical → _teams_post embeds team+channel
            label="Post to a channel",
            message_field="message",
            slot_arg="teams_channel",
            slot_field="teams_target",
        ),
    ),
    # Tier M2 — the default list id isn't known at build time: a lookup PRE-STEP
    # (GET me/todo/lists → wellknown defaultList) runs before the create.
    "ms-todo": ConnectorSpec(
        provider="ms-todo",
        label="Microsoft To Do",
        nango_integration_id="ms-todo",
        auth_type="oauth",
        api_base_url="https://graph.microsoft.com",
        scopes=("Tasks.ReadWrite",),
        action=ConnectorAction(
            endpoint="todo/tasks/create",  # logical → _todo_create targets the default list
            label="Create a task",
            message_field="message",
        ),
    ),
    "ms-onenote": ConnectorSpec(
        provider="ms-onenote",
        label="OneNote",
        nango_integration_id="ms-onenote",
        auth_type="oauth",
        api_base_url="https://graph.microsoft.com",
        # Notes.Create = write-only into the default section — tightest OneNote scope.
        scopes=("Notes.Create",),
        action=ConnectorAction(
            endpoint="onenote/pages/create",  # logical → _onenote_page builds the XHTML
            label="Add a page",
            message_field="message",
        ),
    ),
    "ms-onedrive": ConnectorSpec(
        provider="ms-onedrive",
        label="OneDrive",
        nango_integration_id="ms-onedrive",
        auth_type="oauth",
        api_base_url="https://graph.microsoft.com",
        scopes=("Files.ReadWrite",),
        action=ConnectorAction(
            endpoint="drive/upload",  # logical → _onedrive_upload PUTs a dated .md file
            method="PUT",
            label="Save as a file",
            message_field="message",
        ),
    ),
    # Word/PowerPoint: Graph has NO authoring API for these — Sila builds the
    # .docx/.pptx itself (stdlib OOXML, office_files.py) and PUTs it to OneDrive.
    "ms-word": ConnectorSpec(
        provider="ms-word",
        label="Word",
        nango_integration_id="ms-word",
        auth_type="oauth",
        api_base_url="https://graph.microsoft.com",
        scopes=("Files.ReadWrite",),
        action=ConnectorAction(
            endpoint="word/upload",  # logical → _word_upload builds + PUTs the .docx
            method="PUT",
            label="Save as a Word doc",
            message_field="message",
        ),
    ),
    "ms-powerpoint": ConnectorSpec(
        provider="ms-powerpoint",
        label="PowerPoint",
        nango_integration_id="ms-powerpoint",
        auth_type="oauth",
        api_base_url="https://graph.microsoft.com",
        scopes=("Files.ReadWrite",),
        action=ConnectorAction(
            endpoint="powerpoint/upload",  # logical → _powerpoint_upload builds + PUTs the .pptx
            method="PUT",
            label="Save as a slide deck",
            message_field="message",
        ),
    ),
    # SharePoint = the same Graph drive API as OneDrive, on a SITE's default
    # document library. The site is a real resource picker (one list call, like
    # Slack's channel) — Graph has no per-site delegated scope, so
    # Sites.ReadWrite.All is the tightest available (documented limitation).
    "ms-sharepoint": ConnectorSpec(
        provider="ms-sharepoint",
        label="SharePoint",
        nango_integration_id="ms-sharepoint",
        auth_type="oauth",
        api_base_url="https://graph.microsoft.com",
        scopes=("Sites.ReadWrite.All",),
        resource_pickers=(
            ResourcePicker(
                id="site",
                label="Site",
                list_endpoint="v1.0/sites",
                items_path="value",
                value_key="id",
                label_key="displayName",
                params={"search": "*"},  # Graph lists sites only via search
            ),
        ),
        action=ConnectorAction(
            endpoint="sharepoint/upload",  # logical → _sharepoint_upload PUTs to the site drive
            method="PUT",
            label="Save as a file",
            resource_arg="site",
            resource_field="site_id",
            message_field="message",
        ),
    ),
    # Excel's workbook is captured as a pasted SHARE LINK; a pre-step resolves it
    # to the driveItem id (Graph /shares) before the rows/add.
    "ms-excel": ConnectorSpec(
        provider="ms-excel",
        label="Excel",
        nango_integration_id="ms-excel",
        auth_type="oauth",
        api_base_url="https://graph.microsoft.com",
        scopes=("Files.ReadWrite",),
        action=ConnectorAction(
            endpoint="excel/rows/append",  # logical → _excel_append targets Table1
            label="Append a row",
            message_field="message",
            slot_arg="excel_link",
            slot_field="excel_link",
        ),
    ),
    # Power BI rides the same Azure OAuth as the Graph connectors but its OWN
    # host (api.powerbi.com, Base-Url-Override). Connect-only in the manifest —
    # the push-dataset flow is a SILA data tool (create_powerbi_report), not a
    # flat-JSON publisher action. Needs a work/school account with a (free)
    # Power BI license; see docs/phases/SHAREPOINT_CLICKUP_SETUP.md.
    "powerbi": ConnectorSpec(
        provider="powerbi",
        label="Power BI",
        nango_integration_id="powerbi",
        auth_type="oauth",
        api_base_url="https://api.powerbi.com",
        scopes=("https://analysis.windows.net/powerbi/api/Dataset.ReadWrite.All",),
    ),
    # --- ClickUp — Tier C1 ----------------------------------------------------
    # OAuth grants workspace access as the connecting user (ClickUp has no
    # granular scopes). The target list is captured in the build interview as a
    # pasted list URL/id (the hierarchy team→space→folder→list doesn't fit a
    # one-call picker); build_slots normalizes the URL to the numeric list id.
    "clickup": ConnectorSpec(
        provider="clickup",
        label="ClickUp",
        nango_integration_id="clickup",
        auth_type="oauth",
        api_base_url="https://api.clickup.com",
        scopes=(),  # ClickUp OAuth is all-or-nothing; no scope strings exist
        action=ConnectorAction(
            endpoint="clickup/tasks/create",  # logical → _clickup_task builds the body
            label="Create a task",
            message_field="message",
            slot_arg="clickup_list",
            slot_field="clickup_list",
        ),
    ),
}


def spec_for(provider: str) -> ConnectorSpec | None:
    return CONNECTORS.get(provider.lower())


# Providers whose action body is built by payload_builders (non-flat JSON), so the
# generic node/proxy path routes through the seam. Informational — the builder
# registry is authoritative; this is for docs/tests.
PAYLOAD_BUILT = frozenset(
    {
        "gmail",
        "google-sheets",
        "google-calendar",
        "google-docs",
        "google-chat",
        "google-tasks",
        "outlook-mail",
        "outlook-calendar",
        "ms-teams",
        "ms-todo",
        "ms-onenote",
        "ms-onedrive",
        "ms-excel",
        "ms-word",
        "ms-powerpoint",
        "ms-sharepoint",
        "clickup",
    }
)


def picker_for(spec: ConnectorSpec, picker_id: str) -> ResourcePicker | None:
    return next((p for p in spec.resource_pickers if p.id == picker_id), None)


# Matches an email address so "send to bob@gmail.com" never reads as naming the
# Gmail CONNECTOR — only the user's words do, not an address they typed.
_EMAIL_ADDR_RE = re.compile(r"\S+@\S+")


def providers_named_in(text: str) -> set[str]:
    """Connectors the user NAMED in their own words — deterministic token match on
    the provider key/label (the connector-aware-guidance seam), never model-guessed.
    Used to correct a weak model's integration choice ('via Gmail' must wire the
    Gmail connector, not the generic platform email).

    Longest label first, CONSUMING each match — "outlook calendar" must not also
    name plain "outlook" (label substring collision). O(providers × len)."""
    haystack = _EMAIL_ADDR_RE.sub(" ", (text or "").lower())
    named: set[str] = set()
    candidates = sorted(
        ((spec.label.lower(), key) for key, spec in CONNECTORS.items()),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for label, key in candidates:
        # "google-sheets" also matches as "google sheets" (the label form).
        if key in haystack:
            named.add(key)
            haystack = haystack.replace(key, " ")
        elif label in haystack:
            named.add(key)
            haystack = haystack.replace(label, " ")
    return named
