"""WS-9 — connector authorization (Nango) unit tests.

Covers the manifest, the Nango REST client (mocked transport), and the
provider→proxy mapping. No network, no DB.
"""

import base64
import email
import email.policy
from collections.abc import Callable

import httpx
import pytest

from sila.adapters.connectors.nango_client import NangoClient, NangoError
from sila.services.connectors.manifest import CONNECTORS, picker_for, spec_for
from sila.services.connectors.payload_builders import build_payload


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> NangoClient:
    c = NangoClient("http://nango-server:3003", "nango-secret")
    c._client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0)
    )
    return c


# --- manifest --------------------------------------------------------------


def test_manifest_has_slack_oauth_with_least_scopes() -> None:
    spec = spec_for("slack")
    assert spec is not None
    assert spec.auth_type == "oauth"
    assert spec.nango_integration_id == "slack"
    # Least-privilege — no admin scope.
    assert "chat:write" in spec.scopes
    assert not any("admin" in s for s in spec.scopes)


def test_manifest_lookup_is_case_insensitive_and_misses_safely() -> None:
    assert spec_for("SLACK") is CONNECTORS["slack"]
    assert spec_for("does-not-exist") is None


def test_slack_channel_picker_describes_the_proxied_list_call() -> None:
    spec = spec_for("slack")
    assert spec is not None
    picker = picker_for(spec, "channel")
    assert picker is not None
    assert picker.list_endpoint == "conversations.list"
    assert picker.items_path == "channels"
    assert picker.value_key == "id" and picker.label_key == "name"
    assert picker_for(spec, "nope") is None


# --- Nango client ----------------------------------------------------------


async def test_create_connect_session_returns_token_and_sends_secret() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        seen["path"] = req.url.path
        return httpx.Response(200, json={"data": {"token": "sess_123", "expires_at": "x"}})

    token = await _client(handler).create_connect_session(
        end_user_id="u1", end_user_email="u@x.io", organization_id="w1", integration_ids=["slack"]
    )
    assert token == "sess_123"
    assert seen["auth"] == "Bearer nango-secret"  # secret key sent, never the user's token
    assert seen["path"] == "/connect/sessions"


async def test_create_connect_session_missing_token_raises() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    with pytest.raises(NangoError):
        await _client(handler).create_connect_session(
            end_user_id="u1", end_user_email=None, organization_id=None, integration_ids=["slack"]
        )


async def test_proxy_sets_connection_and_provider_headers() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["conn"] = req.headers.get("connection-id")
        seen["pck"] = req.headers.get("provider-config-key")
        seen["path"] = req.url.path
        return httpx.Response(200, json={"ok": True, "channels": [{"id": "C1", "name": "general"}]})

    out = await _client(handler).proxy(
        connection_id="conn-abc",
        provider_config_key="slack",
        method="GET",
        endpoint="conversations.list",
    )
    assert out["ok"] is True
    assert seen["conn"] == "conn-abc"
    assert seen["pck"] == "slack"
    assert seen["path"] == "/proxy/conversations.list"


async def test_proxy_base_url_override_header() -> None:
    # Per-product hosts (docs/sheets/chat.googleapis.com) ride Nango's
    # Base-Url-Override — the integrations live under the generic google template.
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["override"] = req.headers.get("base-url-override")
        return httpx.Response(200, json={"ok": True})

    await _client(handler).proxy(
        connection_id="c",
        provider_config_key="google-docs",
        method="POST",
        endpoint="v1/documents",
        base_url_override="https://docs.googleapis.com",
    )
    assert seen["override"] == "https://docs.googleapis.com"

    await _client(handler).proxy(
        connection_id="c", provider_config_key="slack", method="POST", endpoint="chat.postMessage"
    )
    assert seen["override"] is None  # no override → header absent


async def test_http_error_raises_nango_error_without_leaking_body() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"secret": "should-not-surface"})

    with pytest.raises(NangoError) as exc:
        await _client(handler).proxy(
            connection_id="c",
            provider_config_key="slack",
            method="POST",
            endpoint="chat.postMessage",
        )
    assert "403" in str(exc.value)
    assert "should-not-surface" not in str(exc.value)


async def test_delete_connection_treats_404_as_already_gone() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    # No raise — revoke is idempotent (fail-closed: the connection is gone either way).
    await _client(handler).delete_connection(connection_id="c", provider_config_key="slack")


def test_unconfigured_client_reports_not_configured() -> None:
    assert NangoClient("http://nango", "").configured is False
    assert NangoClient("http://nango", "k").configured is True


# --- service (fakes, no DB) ------------------------------------------------

import uuid  # noqa: E402

from sila.services.connectors.connector_service import (  # noqa: E402
    ConnectorError,
    ConnectorService,
    _extract_options,
)


class _FakeBindings:
    def __init__(self, binding: object | None = None) -> None:
        self.binding = binding
        self.deleted = False

    async def get(self, **_kw: object) -> object | None:
        return self.binding

    async def list_for_user(self, **_kw: object) -> list[object]:
        return [self.binding] if self.binding else []

    async def delete(self, **_kw: object) -> bool:
        self.deleted = True
        was = self.binding is not None
        self.binding = None
        return was


class _FakeNango:
    def __init__(self, proxy_body: dict | None = None) -> None:
        self.configured = True
        self._proxy_body = proxy_body or {}
        self.deleted: list[str] = []

    async def proxy(self, *, connection_id: str, **_kw: object) -> dict:
        return self._proxy_body

    async def delete_connection(self, *, connection_id: str, **_kw: object) -> None:
        self.deleted.append(connection_id)


class _Binding:
    def __init__(self, provider: str, conn: str) -> None:
        self.provider = provider
        self.nango_connection_id = conn
        self.selected_resources: dict = {}


class _FakeAudit:
    def __init__(self) -> None:
        self.records: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def record(self, *args: object, **kwargs: object) -> None:
        self.records.append((args, kwargs))


def _service(bindings: _FakeBindings, nango: _FakeNango) -> ConnectorService:
    svc = ConnectorService(db=None, nango=nango)  # type: ignore[arg-type]
    svc._bindings = bindings  # type: ignore[assignment]
    svc._audit = _FakeAudit()  # type: ignore[assignment]
    return svc


def test_extract_options_reads_manifest_paths() -> None:
    spec = spec_for("slack")
    assert spec is not None
    picker = picker_for(spec, "channel")
    assert picker is not None
    body = {"channels": [{"id": "C1", "name": "general"}, {"id": "C2", "name": "random"}]}
    options = _extract_options(body, picker)
    assert [(o.value, o.label) for o in options] == [("C1", "general"), ("C2", "random")]


async def test_list_resources_maps_slack_channels() -> None:
    nango = _FakeNango({"channels": [{"id": "C9", "name": "ops"}]})
    svc = _service(_FakeBindings(_Binding("slack", "conn-1")), nango)
    options = await svc.list_resources(
        user_id=uuid.uuid4(), workspace_id=uuid.uuid4(), provider="slack", picker_id="channel"
    )
    assert [(o.value, o.label) for o in options] == [("C9", "ops")]


async def test_list_resources_without_binding_errors() -> None:
    svc = _service(_FakeBindings(None), _FakeNango())
    with pytest.raises(ConnectorError):
        await svc.list_resources(
            user_id=uuid.uuid4(), workspace_id=uuid.uuid4(), provider="slack", picker_id="channel"
        )


async def test_revoke_deletes_connection_and_binding() -> None:
    bindings = _FakeBindings(_Binding("slack", "conn-7"))
    nango = _FakeNango()
    svc = _service(bindings, nango)
    await svc.revoke(user_id=uuid.uuid4(), workspace_id=uuid.uuid4(), provider="slack")
    assert nango.deleted == ["conn-7"]  # upstream connection revoked
    assert bindings.deleted is True  # local binding dropped → fails closed after


async def test_revoke_without_binding_is_idempotent() -> None:
    svc = _service(_FakeBindings(None), _FakeNango())
    # No raise, no upstream call.
    await svc.revoke(user_id=uuid.uuid4(), workspace_id=uuid.uuid4(), provider="slack")


async def test_unknown_provider_is_rejected() -> None:
    svc = _service(_FakeBindings(None), _FakeNango())
    with pytest.raises(ConnectorError):
        await svc.start_connect(
            user_id=uuid.uuid4(), user_email=None, workspace_id=uuid.uuid4(), provider="myspace"
        )


async def test_catalog_marks_connected() -> None:
    svc = _service(_FakeBindings(_Binding("slack", "conn-1")), _FakeNango())
    entries = await svc.catalog(user_id=uuid.uuid4(), workspace_id=uuid.uuid4())
    slack = next(e for e in entries if e.provider == "slack")
    assert slack.connected is True
    # Catalog carries the manifest's pickers so the frontend stays generic.
    assert ("channel", "Channel") in slack.resource_pickers


async def test_execute_proxy_uses_the_binding_connection() -> None:
    nango = _FakeNango({"ok": True})
    svc = _service(_FakeBindings(_Binding("slack", "conn-xyz")), nango)
    out = await svc.execute_proxy(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        provider="slack",
        method="POST",
        endpoint="chat.postMessage",
        data={"channel": "C1", "text": "hi"},
    )
    assert out == {"ok": True}


async def test_execute_proxy_without_binding_fails_closed() -> None:
    svc = _service(_FakeBindings(None), _FakeNango())
    with pytest.raises(ConnectorError):
        await svc.execute_proxy(
            user_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            provider="slack",
            method="POST",
            endpoint="chat.postMessage",
        )


async def test_execute_proxy_emits_one_audit_record_without_bodies() -> None:
    nango = _FakeNango({"ok": True})
    svc = _service(_FakeBindings(_Binding("slack", "conn-1")), nango)
    await svc.execute_proxy(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        provider="slack",
        method="POST",
        endpoint="chat.postMessage",
        data={"channel": "C1", "text": "secret message body"},
    )
    audit = svc._audit  # type: ignore[attr-defined]
    assert len(audit.records) == 1
    _, kwargs = audit.records[0]
    meta = kwargs["metadata"]
    assert meta == {
        "provider": "slack",
        "endpoint": "chat.postMessage",
        "method": "POST",
        "connection_id": "conn-1",
    }
    # No request/response bodies in the audit (the text never lands here).
    assert "secret message body" not in str(audit.records[0])


async def test_execute_proxy_audit_strips_endpoint_query_string() -> None:
    # A future action whose endpoint carries an id/token in the query string must
    # not leak it into the audit — only the path is recorded.
    nango = _FakeNango({"ok": True})
    svc = _service(_FakeBindings(_Binding("slack", "conn-1")), nango)
    await svc.execute_proxy(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        provider="slack",
        method="POST",
        endpoint="messages/send?access_token=SECRET&id=42",
        data={},
    )
    audit = svc._audit  # type: ignore[attr-defined]
    _, kwargs = audit.records[0]
    assert kwargs["metadata"]["endpoint"] == "messages/send"
    assert "SECRET" not in str(audit.records[0])


# --- capability tokens -----------------------------------------------------

from sila.services.connectors import capability  # noqa: E402


def _cap(**over: object) -> str:
    args: dict[str, object] = {
        "workspace_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "provider": "slack",
        "endpoint": "chat.postMessage",
        "connection_id": "conn-1",
    }
    args.update(over)
    return capability.mint(**args)  # type: ignore[arg-type]


def test_capability_roundtrip_carries_endpoint_and_connection() -> None:
    ws, uid = uuid.uuid4(), uuid.uuid4()
    token = capability.mint(
        workspace_id=ws,
        user_id=uid,
        provider="slack",
        endpoint="chat.postMessage",
        connection_id="conn-9",
    )
    cap = capability.verify(token)
    assert cap is not None
    assert (cap.workspace_id, cap.user_id, cap.provider) == (ws, uid, "slack")
    # The token is bound to the action endpoint + the connection — not a bearer.
    assert cap.endpoint == "chat.postMessage"
    assert cap.connection_id == "conn-9"


def test_capability_rejects_tampered_token() -> None:
    token = _cap()
    body, sig = token.split(".", 1)
    assert capability.verify(f"{body}.{sig}x") is None  # bad signature
    assert capability.verify("garbage") is None
    assert capability.verify("") is None


async def test_execute_proxy_rejects_a_rotated_connection() -> None:
    # The capability was minted for conn-OLD; the binding now points at conn-NEW.
    nango = _FakeNango({"ok": True})
    svc = _service(_FakeBindings(_Binding("slack", "conn-NEW")), nango)
    with pytest.raises(ConnectorError):
        await svc.execute_proxy(
            user_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            provider="slack",
            method="POST",
            endpoint="chat.postMessage",
            data={"channel": "C1"},
            expected_connection_id="conn-OLD",
        )


# --- abstraction proof (Slice 7): Google added via manifest only -----------


def test_action_is_declarative_in_the_manifest() -> None:
    # The agent action (endpoint + how the saved resource maps into the payload)
    # lives in the manifest — no Slack-shaped builder code.
    slack = spec_for("slack")
    assert slack is not None and slack.action is not None
    assert slack.action.endpoint == "chat.postMessage"
    assert slack.action.resource_arg == "channel"  # picker id
    assert slack.action.resource_field == "channel"  # payload field
    assert slack.action.message_field == "text"
    # Gmail: a NON-flat action (base64 MIME) wired via the payload builder — the
    # manifest stays declarative (a logical endpoint + message_field), the builder
    # does the encoding. Least-privilege scope.
    gmail = spec_for("gmail")
    assert gmail is not None and gmail.action is not None
    assert gmail.action.endpoint == "users/messages/send"
    assert gmail.action.message_field == "message"
    assert gmail.scopes == ("https://www.googleapis.com/auth/gmail.send",)
    assert not any("readonly" not in s and "drive" in s for s in gmail.scopes)  # no drive scope


def test_new_connector_added_by_manifest_only() -> None:
    # Gmail flows through the SAME generic code: spec lookup + catalog. No
    # gmail-specific service code — only a manifest entry + a payload builder.
    gmail = spec_for("gmail")
    assert gmail is not None and gmail.auth_type == "oauth"
    assert gmail.nango_integration_id == "gmail"


def test_resource_extraction_is_generic() -> None:
    # The picker extraction is provider-agnostic (proven with a synthetic picker).
    from sila.services.connectors.manifest import ResourcePicker

    picker = ResourcePicker(id="file", label="File", list_endpoint="x", items_path="files")
    body = {"files": [{"id": "f1", "name": "Q3 Report"}, {"id": "f2", "name": "Notes"}]}
    options = _extract_options(body, picker)
    assert [(o.value, o.label) for o in options] == [("f1", "Q3 Report"), ("f2", "Notes")]


async def test_catalog_lists_oauth_connectors() -> None:
    svc = _service(_FakeBindings(None), _FakeNango())
    entries = await svc.catalog(user_id=uuid.uuid4(), workspace_id=uuid.uuid4())
    providers = {e.provider for e in entries}
    assert {"slack", "gmail"} <= providers


# --- payload builders (WS-9 — Google non-flat actions) ----------------------


def test_flat_action_is_identity() -> None:
    # Slack-shaped: no registered builder → the node's body passes through unchanged.
    ep, body = build_payload("slack", "chat.postMessage", {"channel": "C1", "text": "hi"})
    assert ep == "chat.postMessage"
    assert body == {"channel": "C1", "text": "hi"}


def test_gmail_builds_base64url_mime() -> None:
    ep, body = build_payload(
        "gmail",
        "users/messages/send",
        {"to": "me@company.om", "subject": "التقرير", "message": "مرحبا — النص"},
    )
    assert ep == "gmail/v1/users/me/messages/send"
    assert set(body) == {"raw"}
    # Decodes as a real RFC-2822 message with the unicode subject/body preserved.
    from email.header import decode_header, make_header

    raw = base64.urlsafe_b64decode(body["raw"])
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert msg["To"] == "me@company.om"
    assert "التقرير" in str(make_header(decode_header(msg["Subject"])))
    assert "مرحبا" in msg.get_content()


def test_gmail_defaults_subject_and_handles_no_recipient() -> None:
    ep, body = build_payload("gmail", "users/messages/send", {"message": "body only"})
    raw = base64.urlsafe_b64decode(body["raw"])
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert msg["Subject"] == "Your summary"
    assert msg["To"] is None  # send-to-self / caller-less is allowed


def test_sheets_append_embeds_id_in_path_and_wraps_values() -> None:
    ep, body = build_payload(
        "google-sheets",
        "values/append",
        {"spreadsheet_id": "sheet_ABC", "message": "row text"},
    )
    assert ep.startswith("v4/spreadsheets/sheet_ABC/values/")
    assert ep.endswith(":append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS")
    assert body == {"values": [["row text"]]}


def test_calendar_insert_builds_event_on_chosen_calendar() -> None:
    ep, body = build_payload(
        "google-calendar",
        "events/insert",
        {
            "calendar_id": "cal_1",
            "title": "Sync",
            "message": "agenda",
            "start": "2026-07-05T09:00:00Z",
            "end": "2026-07-05T09:30:00Z",
        },
    )
    assert ep == "calendar/v3/calendars/cal_1/events"
    assert body["summary"] == "Sync" and body["description"] == "agenda"
    assert body["start"] == {"dateTime": "2026-07-05T09:00:00Z"}


def test_calendar_defaults_to_primary_without_a_calendar() -> None:
    ep, _ = build_payload("google-calendar", "events/insert", {"message": "x"})
    assert ep == "calendar/v3/calendars/primary/events"


def test_docs_follow_up_inserts_body_into_created_doc() -> None:
    # Docs has no create-with-content: the composite action's second call inserts
    # the generated text into the fresh documentId (built from the FIRST response,
    # server-side — never trusted from the caller).
    from sila.services.connectors.payload_builders import follow_up_payload

    follow = follow_up_payload(
        "google-docs",
        "documents/create",
        {"message": "Daily digest\n- point"},
        {"documentId": "D1"},
    )
    assert follow is not None
    endpoint, body = follow
    assert endpoint == "v1/documents/D1:batchUpdate"
    insert = body["requests"][0]["insertText"]
    assert insert["location"] == {"index": 1}
    assert insert["text"].startswith("Daily digest")
    # No documentId (create failed upstream) or no text → nothing to do.
    assert follow_up_payload("google-docs", "documents/create", {"message": "x"}, {}) is None
    assert follow_up_payload("google-docs", "documents/create", {}, {"documentId": "D1"}) is None
    # Non-composite actions have no follow-up.
    assert follow_up_payload("gmail", "users/messages/send", {"message": "x"}, {"id": "m"}) is None


async def test_execute_proxy_runs_the_docs_follow_up() -> None:
    # One logical google-docs action → TWO provider calls: create, then the
    # batchUpdate that writes the body.
    class _RecordingNango(_FakeNango):
        def __init__(self) -> None:
            super().__init__({"documentId": "D9"})
            self.calls: list[str] = []
            self.overrides: list[object] = []

        async def proxy(self, *, connection_id: str, **kw: object) -> dict:
            self.calls.append(str(kw.get("endpoint")))
            self.overrides.append(kw.get("base_url_override"))
            return await super().proxy(connection_id=connection_id, **kw)

    nango = _RecordingNango()
    svc = _service(_FakeBindings(_Binding("google-docs", "conn-d")), nango)
    await svc.execute_proxy(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        provider="google-docs",
        method="POST",
        endpoint="documents/create",
        data={"message": "Digest body"},
    )
    assert nango.calls == ["v1/documents", "v1/documents/D9:batchUpdate"]
    # Both calls carry the per-product host (the generic google template's base
    # www.googleapis.com 404s for Docs — live-hit; the email chain died with it).
    assert nango.overrides == ["https://docs.googleapis.com", "https://docs.googleapis.com"]


def test_per_product_api_base_urls() -> None:
    # Products NOT on the legacy shared host need the override; the proven
    # shared-host products must stay untouched (gmail send is live-proven).
    for provider, base in (
        ("google-docs", "https://docs.googleapis.com"),
        ("google-sheets", "https://sheets.googleapis.com"),
        ("google-chat", "https://chat.googleapis.com"),
    ):
        spec = spec_for(provider)
        assert spec is not None and spec.api_base_url == base
    for provider in ("gmail", "google-calendar", "google-tasks", "slack"):
        spec = spec_for(provider)
        assert spec is not None and spec.api_base_url is None


def test_docs_create_titles_from_text_when_absent() -> None:
    ep, body = build_payload(
        "google-docs", "documents/create", {"message": "Weekly report\nline two"}
    )
    assert ep == "v1/documents"
    assert body == {"title": "Weekly report"}


def test_all_google_connectors_are_least_privilege_single_scope() -> None:
    # Per-product: each Google connector holds exactly ONE minimal scope and its own
    # Nango integration — an agent that sends mail can't touch Sheets/Calendar/Docs.
    for provider in ("gmail", "google-sheets", "google-calendar", "google-docs"):
        spec = spec_for(provider)
        assert spec is not None and spec.auth_type == "oauth"
        assert spec.nango_integration_id == provider
        assert len(spec.scopes) == 1
        assert spec.action is not None and spec.action.message_field == "message"
        assert not any("full" in s or ".readonly" in s for s in spec.scopes)


def test_tier1_remainder_registered_least_privilege() -> None:
    # The remaining Tier-1 Google apps (roadmap 2026-07-03): all connectable with ONE
    # scope each. Drive (multipart transport), Contacts (no sensible publisher
    # action), and Forms (READ class) are connect-only — action design pending.
    for provider, has_action in (
        ("google-drive", False),
        ("google-chat", True),
        ("google-contacts", False),
        ("google-tasks", True),
        ("google-forms", False),
    ):
        spec = spec_for(provider)
        assert spec is not None and spec.auth_type == "oauth"
        assert spec.nango_integration_id == provider
        assert len(spec.scopes) == 1
        assert (spec.action is not None) is has_action


def test_chat_post_embeds_space_in_path() -> None:
    ep, body = build_payload(
        "google-chat", "spaces/messages/create", {"space": "spaces/AAA1", "message": "hi"}
    )
    assert ep == "v1/spaces/AAA1/messages"
    assert body == {"text": "hi"}
    # A bare space id is prefixed defensively.
    ep2, _ = build_payload(
        "google-chat", "spaces/messages/create", {"space": "AAA1", "message": "x"}
    )
    assert ep2 == "v1/spaces/AAA1/messages"


def test_tasks_insert_titles_first_line_notes_rest() -> None:
    ep, body = build_payload(
        "google-tasks", "tasks/insert", {"message": "Do the thing\ndetails here"}
    )
    assert ep == "tasks/v1/lists/@default/tasks"
    assert body == {"title": "Do the thing", "notes": "details here"}
    # Single-line text → title only, no empty notes key.
    _, body2 = build_payload("google-tasks", "tasks/insert", {"message": "Just this"})
    assert body2 == {"title": "Just this"}


# --- interview target capture (build → connector node data) ------------------


def test_interview_targets_are_declared_in_the_manifest() -> None:
    # Picker-less connectors declare which interview slot captures their target and
    # which payload field it fills (what the payload builder reads) — still no code.
    gmail = spec_for("gmail")
    sheets = spec_for("google-sheets")
    assert gmail is not None and gmail.action is not None
    assert (gmail.action.slot_arg, gmail.action.slot_field) == ("gmail_to", "to")
    assert sheets is not None and sheets.action is not None
    assert (sheets.action.slot_arg, sheets.action.slot_field) == ("sheet_id", "spreadsheet_id")


def test_manifest_slot_args_exist_as_interview_slots() -> None:
    # Consistency invariant: every slot_arg the manifest names must be a real
    # destination slot for that provider in the publisher interview — otherwise the
    # gate never asks it and the node data stays empty.
    from sila.services.connectors.manifest import CONNECTORS
    from sila.services.sila.build_slots import REQUIRED_SLOTS, SCHEDULED_PUBLISHER

    slots = {s.name: s for s in REQUIRED_SLOTS[SCHEDULED_PUBLISHER]}
    for cspec in CONNECTORS.values():
        if cspec.action and cspec.action.slot_arg:
            assert cspec.action.slot_field, f"{cspec.provider}: slot_arg without slot_field"
            slot = slots.get(cspec.action.slot_arg)
            assert slot is not None, f"{cspec.provider}: {cspec.action.slot_arg} not a slot"
            assert slot.destination_for == cspec.provider


async def test_build_injects_interview_targets_into_node_data(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    # The captured slot values must land in the connector node data under the payload
    # field the builder reads — gmail "to", sheets "spreadsheet_id" (URL → bare id).
    from sila.core.config import settings
    from sila.core.constants import Role
    from sila.db.repositories import connectors as conn_repo
    from sila.services.sila.tool_registry import ToolContext, _resolve_oauth_connectors

    monkeypatch.setattr(settings, "nango_secret_key", "k")

    class _Repo:
        def __init__(self, _db: object) -> None: ...

        async def get(self, **kw: object) -> _Binding:
            return _Binding(str(kw["provider"]), f"conn-{kw['provider']}")

    monkeypatch.setattr(conn_repo, "ConnectorBindingRepository", _Repo)
    from sila.adapters.auth.base import UserResult

    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.BUILDER,
    )
    filled = {
        "gmail_to": "boss@example.com",
        "sheet_id": "https://docs.google.com/spreadsheets/d/1AbC_d-EfG9/edit#gid=0",
    }
    nodes, _, interrupt = await _resolve_oauth_connectors(ctx, ["gmail", "google-sheets"], filled)
    assert interrupt is None
    assert nodes["gmail"].data["to"] == "boss@example.com"
    assert nodes["google-sheets"].data["spreadsheet_id"] == "1AbC_d-EfG9"


def test_mail_builders_attach_the_exported_pdf() -> None:
    # "Email me the FILE" — the proxy injects (attachment_b64, attachment_name);
    # both mail builders must carry it (Gmail: MIME part; Outlook: fileAttachment).
    blob = base64.b64encode(b"%PDF-fake").decode()
    _, body = build_payload(
        "gmail",
        "users/messages/send",
        {"to": "a@b.om", "message": "digest", "attachment_b64": blob, "attachment_name": "d.pdf"},
    )
    raw = base64.urlsafe_b64decode(body["raw"])
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    parts = list(msg.iter_attachments())
    assert len(parts) == 1 and parts[0].get_filename() == "d.pdf"
    assert parts[0].get_content_type() == "application/pdf"
    assert parts[0].get_payload(decode=True) == b"%PDF-fake"

    _, obody = build_payload(
        "outlook-mail",
        "me/sendMail",
        {"to": "a@b.om", "message": "digest", "attachment_b64": blob, "attachment_name": "d.pdf"},
    )
    att = obody["message"]["attachments"][0]
    assert att["name"] == "d.pdf" and att["contentBytes"] == blob
    # No attachment injected → no attachments key (Graph rejects empty lists).
    _, plain = build_payload("outlook-mail", "me/sendMail", {"to": "a@b.om", "message": "x"})
    assert "attachments" not in plain["message"]


async def test_execute_proxy_attaches_the_doc_pdf() -> None:
    # A gmail send whose data carries attach_doc_id: the proxy exports the doc as
    # PDF via the USER'S OWN google-docs binding and the outgoing MIME carries it.
    class _AttachNango(_FakeNango):
        def __init__(self) -> None:
            super().__init__({"id": "m1"})
            self.sent_json: dict | None = None
            self.raw_calls: list[str] = []

        async def proxy(self, *, connection_id: str, **kw: object) -> dict:
            self.sent_json = kw.get("json")  # type: ignore[assignment]
            return await super().proxy(connection_id=connection_id, **kw)

        async def proxy_raw(self, *, connection_id: str, **kw: object) -> bytes:
            self.raw_calls.append(str(kw.get("endpoint")))
            return b"%PDF-fake"

    nango = _AttachNango()
    svc = _service(_FakeBindings(_Binding("gmail", "conn-g")), nango)
    await svc.execute_proxy(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        provider="gmail",
        method="POST",
        endpoint="users/messages/send",
        data={"to": "a@b.om", "message": "digest", "attach_doc_id": "D77"},
    )
    assert nango.raw_calls == ["drive/v3/files/D77/export"]
    raw = base64.urlsafe_b64decode(nango.sent_json["raw"])  # type: ignore[index]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert [p.get_content_type() for p in msg.iter_attachments()] == ["application/pdf"]


async def test_proxy_raw_returns_bytes() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers.get("base-url-override") == "https://www.googleapis.com"
        return httpx.Response(200, content=b"%PDF-bytes")

    out = await _client(handler).proxy_raw(
        connection_id="c",
        provider_config_key="google-docs",
        method="GET",
        endpoint="drive/v3/files/X/export",
        params={"mimeType": "application/pdf"},
        base_url_override="https://www.googleapis.com",
    )
    assert out == b"%PDF-bytes"


def test_outlook_m1_manifest_entries() -> None:
    # Tier M1: both under the generic `microsoft` Nango template → explicit Graph
    # host (the Google generic-template 404 lesson); outlook-mail captures its
    # recipient via the interview like Gmail. Mail.Read powers read_outlook_emails.
    mail = spec_for("outlook-mail")
    assert mail is not None and mail.auth_type == "oauth"
    assert mail.api_base_url == "https://graph.microsoft.com"
    assert mail.scopes == ("Mail.Send", "Mail.Read")
    assert mail.action is not None
    assert (mail.action.slot_arg, mail.action.slot_field) == ("outlook_to", "to")
    cal = spec_for("outlook-calendar")
    assert cal is not None and cal.api_base_url == "https://graph.microsoft.com"
    assert cal.scopes == ("Calendars.ReadWrite",)
    assert cal.action is not None and cal.action.endpoint == "me/events"
    teams = spec_for("ms-teams")
    # Meeting-transcript scopes were added for read_meeting_transcript; existing
    # connections must reconnect to gain them (mirrored in the Nango integration).
    # Channel.ReadBasic.All is NOT optional: Team.ReadBasic.All lists the teams
    # you are in, but GET /teams/{id}/channels 403s without this one, so channel
    # discovery — and therefore any Teams-posting agent — could not be built.
    assert teams is not None and teams.scopes == (
        "ChannelMessage.Send",
        "ChannelMessage.Read.All",
        "Team.ReadBasic.All",
        "Channel.ReadBasic.All",
        "OnlineMeetings.Read",
        "OnlineMeetingTranscript.Read.All",
    )


def test_outlook_send_builds_graph_payload() -> None:
    ep, body = build_payload(
        "outlook-mail", "me/sendMail", {"to": "boss@corp.om", "message": "The digest"}
    )
    assert ep == "v1.0/me/sendMail"
    msg = body["message"]
    assert msg["toRecipients"] == [{"emailAddress": {"address": "boss@corp.om"}}]
    assert msg["body"] == {"contentType": "Text", "content": "The digest"}
    assert body["saveToSentItems"] is True


def test_outlook_event_defaults_required_start_end() -> None:
    # Graph REQUIRES start+end — absent from the build, a default block is used.
    ep, body = build_payload("outlook-calendar", "me/events", {"message": "agenda"})
    assert ep == "v1.0/me/events"
    assert body["body"]["content"] == "agenda"
    assert body["start"]["timeZone"] == "UTC" and body["end"]["timeZone"] == "UTC"
    assert body["start"]["dateTime"] and body["end"]["dateTime"]
    # Explicit times pass through untouched.
    _, body2 = build_payload(
        "outlook-calendar",
        "me/events",
        {"message": "x", "start": "2026-08-01T09:00:00", "end": "2026-08-01T09:30:00"},
    )
    assert body2["start"]["dateTime"] == "2026-08-01T09:00:00"


def test_named_provider_label_collision_consumes_longest_first() -> None:
    from sila.services.connectors.manifest import providers_named_in

    # "outlook calendar" must NOT also name plain Outlook (label substring).
    assert providers_named_in("add a block to my outlook calendar") == {"outlook-calendar"}
    assert providers_named_in("send the digest via outlook") == {"outlook-mail"}


def test_teams_post_builds_channel_path_from_target() -> None:
    ep, body = build_payload(
        "ms-teams",
        "teams/messages/post",
        {"teams_target": "aaaa-team-guid/19:abc@thread.tacv2", "message": "update"},
    )
    assert ep == "v1.0/teams/aaaa-team-guid/channels/19%3Aabc%40thread.tacv2/messages"
    assert body == {"body": {"contentType": "text", "content": "update"}}


def test_todo_pre_step_resolves_default_list() -> None:
    from sila.services.connectors.payload_builders import apply_pre_step, pre_step_for

    assert pre_step_for("ms-todo", "todo/tasks/create", {}) == ("GET", "v1.0/me/todo/lists")
    lookup = {
        "value": [
            {"id": "L1", "wellknownListName": "flaggedEmails"},
            {"id": "L2", "wellknownListName": "defaultList"},
        ]
    }
    data = apply_pre_step(
        "ms-todo", "todo/tasks/create", {"message": "Buy milk\nfrom shop"}, lookup
    )
    assert data["list_id"] == "L2"
    ep, body = build_payload("ms-todo", "todo/tasks/create", data)
    assert ep == "v1.0/me/todo/lists/L2/tasks"
    assert body["title"] == "Buy milk"
    assert body["body"] == {"content": "from shop", "contentType": "text"}
    # No pre-step registered → identity.
    assert pre_step_for("gmail", "users/messages/send", {}) is None


async def test_execute_proxy_runs_the_todo_pre_step() -> None:
    class _TodoNango(_FakeNango):
        def __init__(self) -> None:
            super().__init__({"value": [{"id": "L9", "wellknownListName": "defaultList"}]})
            self.endpoints: list[str] = []

        async def proxy(self, *, connection_id: str, **kw: object) -> dict:
            self.endpoints.append(str(kw.get("endpoint")))
            return await super().proxy(connection_id=connection_id, **kw)

    nango = _TodoNango()
    svc = _service(_FakeBindings(_Binding("ms-todo", "conn-t")), nango)
    await svc.execute_proxy(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        provider="ms-todo",
        method="POST",
        endpoint="todo/tasks/create",
        data={"message": "Follow up"},
    )
    assert nango.endpoints == ["v1.0/me/todo/lists", "v1.0/me/todo/lists/L9/tasks"]


def test_onenote_page_builds_escaped_xhtml_raw_body() -> None:
    ep, body = build_payload("ms-onenote", "onenote/pages/create", {"message": "Line <1>\nLine 2"})
    assert ep == "v1.0/me/onenote/pages"
    assert body["__content_type__"] == "application/xhtml+xml"
    assert "<p>Line &lt;1&gt;</p><p>Line 2</p>" in body["__content__"]  # escaped, never raw


def test_onedrive_upload_puts_dated_markdown() -> None:
    ep, body = build_payload("ms-onedrive", "drive/upload", {"message": "digest text"})
    assert ep.startswith("v1.0/me/drive/root:/digest-") and ep.endswith(".md:/content")
    assert body == {"__content__": "digest text", "__content_type__": "text/markdown"}


def test_excel_share_link_pre_step_and_append() -> None:
    from sila.services.connectors.payload_builders import apply_pre_step, pre_step_for

    link = "https://1drv.ms/x/s!Abc123"
    pre = pre_step_for("ms-excel", "excel/rows/append", {"excel_link": link})
    assert pre is not None and pre[0] == "GET"
    assert pre[1].startswith("v1.0/shares/u!") and pre[1].endswith("/driveItem")
    assert "=" not in pre[1]  # base64url, padding stripped (Graph share-token rule)
    data = apply_pre_step(
        "ms-excel", "excel/rows/append", {"excel_link": link, "message": "row"}, {"id": "IT9"}
    )
    ep, body = build_payload("ms-excel", "excel/rows/append", data)
    assert ep == "v1.0/me/drive/items/IT9/workbook/tables/Table1/rows/add"
    assert body == {"values": [["row"]]}
    # No link captured → no pre-step to run.
    assert pre_step_for("ms-excel", "excel/rows/append", {}) is None


async def test_execute_proxy_sends_raw_content_for_onenote() -> None:
    class _RawNango(_FakeNango):
        def __init__(self) -> None:
            super().__init__({"id": "page1"})
            self.kw: dict | None = None

        async def proxy(self, *, connection_id: str, **kw: object) -> dict:
            self.kw = kw  # type: ignore[assignment]
            return await super().proxy(connection_id=connection_id, **kw)

    nango = _RawNango()
    svc = _service(_FakeBindings(_Binding("ms-onenote", "conn-n")), nango)
    await svc.execute_proxy(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        provider="ms-onenote",
        method="POST",
        endpoint="onenote/pages/create",
        data={"message": "note body"},
    )
    assert nango.kw is not None
    assert nango.kw.get("json") is None  # raw body, not JSON
    assert "note body" in str(nango.kw.get("content"))
    assert nango.kw.get("content_type") == "application/xhtml+xml"


def test_word_and_powerpoint_build_valid_ooxml_uploads() -> None:
    # Graph has no Word/PPT authoring API — Sila builds the OOXML itself. The
    # output must be a well-formed zip whose parts parse as XML with the text
    # escaped (model output can never inject markup).
    import io
    import zipfile
    from xml.etree import ElementTree

    ep, body = build_payload(
        "ms-word", "word/upload", {"message": "Line <1>\nLine 2", "title": "T&D"}
    )
    assert ep.startswith("v1.0/me/drive/root:/digest-") and ep.endswith(".docx:/content")
    assert body["__content_type__"].endswith("wordprocessingml.document")
    with zipfile.ZipFile(io.BytesIO(body["__content__"])) as z:
        assert z.testzip() is None
        doc = z.read("word/document.xml").decode()
        ElementTree.fromstring(doc)  # well-formed
        assert "Line &lt;1&gt;" in doc and "T&amp;D" in doc

    ep2, body2 = build_payload("ms-powerpoint", "powerpoint/upload", {"message": "Point A"})
    assert ep2.endswith(".pptx:/content")
    assert body2["__content_type__"].endswith("presentationml.presentation")
    with zipfile.ZipFile(io.BytesIO(body2["__content__"])) as z:
        names = set(z.namelist())
        assert {
            "[Content_Types].xml",
            "ppt/presentation.xml",
            "ppt/slides/slide1.xml",
            "ppt/slideMasters/slideMaster1.xml",
            "ppt/theme/theme1.xml",
        } <= names
        for name in names:
            if name.endswith(".xml") or name.endswith(".rels"):
                ElementTree.fromstring(z.read(name))  # every part well-formed
        assert "Point A" in z.read("ppt/slides/slide1.xml").decode()


def test_sharepoint_upload_targets_the_picked_site_drive() -> None:
    # SharePoint = OneDrive's drive PUT, on the picked site. Text → dated .md;
    # a pre-built deck (__deck_bytes__) → .pptx as-is.
    site = "contoso.sharepoint.com,a1b2,c3d4"  # Graph's host,siteGuid,webGuid form
    ep, body = build_payload(
        "ms-sharepoint", "sharepoint/upload", {"site_id": site, "message": "digest"}
    )
    assert ep.startswith(f"v1.0/sites/{site}/drive/root:/digest-")  # commas kept
    assert ep.endswith(".md:/content")
    assert body == {"__content__": "digest", "__content_type__": "text/markdown"}

    ep2, body2 = build_payload(
        "ms-sharepoint",
        "sharepoint/upload",
        {"site_id": "S1", "__deck_bytes__": b"PK\x03\x04deck", "filename": "q3.pptx"},
    )
    assert ep2 == "v1.0/sites/S1/drive/root:/q3.pptx:/content"
    assert body2["__content__"] == b"PK\x03\x04deck"
    assert body2["__content_type__"].endswith("presentationml.presentation")


def test_sharepoint_site_picker_lists_via_search() -> None:
    # Graph exposes sites only through ?search=; the picker reads value/[].
    spec = spec_for("ms-sharepoint")
    assert spec is not None and spec.action is not None
    assert (spec.action.resource_arg, spec.action.resource_field) == ("site", "site_id")
    picker = spec.resource_pickers[0]
    assert picker.list_endpoint == "v1.0/sites" and picker.params.get("search") == "*"
    assert (picker.items_path, picker.value_key, picker.label_key) == (
        "value",
        "id",
        "displayName",
    )


def test_clickup_task_titles_from_first_line_body_is_markdown() -> None:
    # First line (heading stripped) names the task; the whole text is the body.
    ep, body = build_payload(
        "clickup",
        "clickup/tasks/create",
        {"clickup_list": "901234", "message": "# Fix login bug\nUsers see a 500 on submit."},
    )
    assert ep == "api/v2/list/901234/task"
    assert body["name"] == "Fix login bug"
    assert body["markdown_description"].startswith("# Fix login bug")


def test_clickup_list_url_normalizes_to_id() -> None:
    from sila.services.sila.build_slots import normalize_destination

    url = "https://app.clickup.com/9012/v/li/901234567"
    assert normalize_destination("clickup_list", url) == "901234567"
    # A bare id passes through unchanged.
    assert normalize_destination("clickup_list", "901234567") == "901234567"


def test_providers_named_in_matches_words_not_addresses() -> None:
    from sila.services.connectors.manifest import providers_named_in

    assert providers_named_in("email me a daily digest via Gmail") == {"gmail"}
    assert providers_named_in("log rows into Google Sheets weekly") == {"google-sheets"}
    # An email ADDRESS must not read as naming the Gmail connector.
    assert providers_named_in("send the summary to bob@gmail.com") == set()
    assert providers_named_in("just answer HR questions") == set()


def test_destination_in_accepts_any_manifest_connector() -> None:
    # The build path emits a destination for EVERY wired connector; the edit
    # endpoint's schema must accept them all (a hardcoded whitelist 422'd the
    # save on a google-docs agent). The manifest is the single source of truth.
    from sila.api.v1.agents import DestinationIn

    for provider in ("slack", "email", "gmail", "google-docs", "google-calendar", "google-chat"):
        assert DestinationIn(type=provider).type == provider
    with pytest.raises(ValueError):
        DestinationIn(type="myspace")


def test_destination_label_per_type() -> None:
    # The agent card's "posts to …" label — shared by the build brief and the edit path.
    from sila.services.sila.workflow_assembly import destination_label

    assert destination_label({"type": "slack", "channel_name": "general"}) == "#general"
    assert destination_label({"type": "email", "to": "a@b.com"}) == "a@b.com"
    assert destination_label({"type": "gmail", "to": "g@b.com"}) == "g@b.com"
    assert (
        destination_label({"type": "google-sheets", "spreadsheet_id": "1234567890ABCDEF"})
        == "Google Sheet 1234567890AB…"
    )
