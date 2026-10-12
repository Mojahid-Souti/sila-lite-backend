"""Connector service (WS-9) — ties the manifest, the Nango client, and the
per-user binding repository together.

Flow: mint a Connect session → user authorizes in the Connect UI → frontend
posts the resulting connection_id → we persist a *binding* (reference only) →
resource pickers populate via the proxy → execution calls go through the proxy
with the user's connection. No third-party token ever touches Sila.

Complexity: each method is O(1) DB + O(1) Nango round-trip.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog

from sila.adapters.connectors.nango_client import NangoClient, NangoError
from sila.core.config import settings
from sila.core.constants import Action, Outcome, ResourceType
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.connectors import ConnectorBindingRepository
from sila.services.connectors.manifest import (
    ConnectorSpec,
    ResourcePicker,
    picker_for,
    spec_for,
)
from sila.services.connectors.payload_builders import (
    apply_pre_step,
    build_payload,
    follow_up_payload,
    pre_step_for,
)

log = structlog.get_logger(__name__)


class ConnectorError(RuntimeError):
    """A connector operation failed (unknown provider, Nango down, no binding)."""


class NoMessagesError(ConnectorError):
    """A mailbox read worked but nothing matched the window — an answer, not a failure."""


@dataclass(frozen=True)
class ConnectInit:
    provider: str
    integration_id: str
    session_token: str


@dataclass(frozen=True)
class ResourceOption:
    value: str
    label: str


@dataclass(frozen=True)
class CatalogEntry:
    provider: str
    label: str
    auth_type: str
    scopes: tuple[str, ...]
    connected: bool
    selected_resources: dict[str, Any]
    # Pickers the frontend renders after connect, e.g. [("channel", "Channel")].
    resource_pickers: tuple[tuple[str, str], ...]


def make_nango() -> NangoClient:
    return NangoClient(settings.nango_server_url, settings.nango_secret_key)


class ConnectorService:
    def __init__(self, db: Any, nango: NangoClient | None = None) -> None:
        self._db = db
        self._nango = nango or make_nango()
        self._bindings = ConnectorBindingRepository(db)
        self._audit = AuditRepository(db)

    # -- connect ---------------------------------------------------------

    async def start_connect(
        self, *, user_id: uuid.UUID, user_email: str | None, workspace_id: uuid.UUID, provider: str
    ) -> ConnectInit:
        spec = self._require_oauth_spec(provider)
        self._require_nango()
        try:
            token = await self._nango.create_connect_session(
                end_user_id=str(user_id),
                end_user_email=user_email,
                organization_id=str(workspace_id),
                integration_ids=[spec.nango_integration_id],
            )
        except NangoError as err:
            raise ConnectorError(f"Could not start the connection: {err}") from err
        return ConnectInit(
            provider=spec.provider, integration_id=spec.nango_integration_id, session_token=token
        )

    async def persist_binding(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        provider: str,
        connection_id: str,
    ) -> None:
        spec = self._require_oauth_spec(provider)
        await self._bindings.upsert(
            user_id=user_id,
            workspace_id=workspace_id,
            provider=spec.provider,
            nango_connection_id=connection_id,
        )
        await self._audit.record(
            Action.CONNECTOR_CONNECT,
            Outcome.SUCCESS,
            actor_user_id=user_id,
            actor_workspace_id=workspace_id,
            resource_type=ResourceType.CONNECTOR_BINDING,
            metadata={"provider": spec.provider},
        )

    # -- resource pickers ------------------------------------------------

    async def list_resources(
        self, *, user_id: uuid.UUID, workspace_id: uuid.UUID, provider: str, picker_id: str
    ) -> list[ResourceOption]:
        spec = self._require_oauth_spec(provider)
        picker = picker_for(spec, picker_id)
        if picker is None:
            raise ConnectorError(f"Unknown resource picker '{picker_id}' for {provider}")
        binding = await self._bindings.get(
            user_id=user_id, workspace_id=workspace_id, provider=spec.provider
        )
        if binding is None:
            raise ConnectorError(f"{spec.label} is not connected")
        self._require_nango()
        try:
            body = await self._nango.proxy(
                connection_id=binding.nango_connection_id,
                provider_config_key=spec.nango_integration_id,
                method=picker.list_method,
                endpoint=picker.list_endpoint,
                params=dict(picker.params) or None,
            )
        except NangoError as err:
            raise ConnectorError(f"Could not load {picker.label.lower()} options: {err}") from err
        return _extract_options(body, picker)

    async def save_resource_choice(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        provider: str,
        resources: dict[str, Any],
    ) -> None:
        spec = self._require_oauth_spec(provider)
        updated = await self._bindings.set_resources(
            user_id=user_id,
            workspace_id=workspace_id,
            provider=spec.provider,
            selected_resources=resources,
        )
        if updated is None:
            raise ConnectorError(f"{spec.label} is not connected")

    # -- execution (n8n → Sila → Nango proxy) ----------------------------

    async def _slack_ensure_member(
        self, connection_id: str, integration_id: str, channel: str
    ) -> None:
        """Self-join the target channel before posting, so a freshly-connected bot
        can post without a manual invite (needs the channels:join scope). Idempotent —
        already-a-member is fine. A private channel can't be auto-joined → raise a
        clear, actionable error instead of letting the post fail with not_in_channel."""
        if not channel:
            return
        try:
            res = await self._nango.proxy(
                connection_id=connection_id,
                provider_config_key=integration_id,
                method="POST",
                endpoint="conversations.join",
                json={"channel": channel},
            )
        except NangoError:
            return  # best-effort — let the post run; it surfaces a real failure if needed
        if res.get("ok") or res.get("error") == "already_in_channel":
            return
        err = res.get("error")
        if err in ("method_not_supported_for_channel_type", "channel_not_found"):
            raise ConnectorError(
                "The bot can't auto-join this channel (it looks private). Invite the bot "
                "to the channel in Slack, then it will post."
            )
        if err == "missing_scope":
            raise ConnectorError(
                "The Slack app is missing the channels:join scope. Add it to the app + the "
                "Nango integration and reconnect, then the bot can post to new channels."
            )
        # Any other join error: don't block — the post itself will fail clearly if the
        # bot genuinely isn't a member.

    async def _export_doc_pdf(
        self, *, user_id: uuid.UUID, workspace_id: uuid.UUID, doc_id: str
    ) -> bytes | None:
        """Export ONE of the user's app-created Google Docs as PDF via THEIR OWN
        google-docs binding (drive.file scope — the export only works for files
        this app created, so an injected id can't exfiltrate arbitrary Drive
        content). Returns None when docs isn't connected or the export fails —
        the caller degrades to link-only mail. Audited like any proxied call."""
        docs_spec = spec_for("google-docs")
        if docs_spec is None:
            return None
        binding = await self._bindings.get(
            user_id=user_id, workspace_id=workspace_id, provider="google-docs"
        )
        if binding is None:
            return None
        outcome = Outcome.FAILURE
        try:
            pdf = await self._nango.proxy_raw(
                connection_id=binding.nango_connection_id,
                provider_config_key=docs_spec.nango_integration_id,
                method="GET",
                endpoint=f"drive/v3/files/{doc_id}/export",
                params={"mimeType": "application/pdf"},
                # Drive lives on the legacy shared host (unlike the Docs API).
                base_url_override="https://www.googleapis.com",
            )
            outcome = Outcome.SUCCESS
            return pdf
        except NangoError:
            return None
        finally:
            await self._audit.record(
                Action.CONNECTOR_PROXY,
                outcome,
                actor_user_id=user_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.CONNECTOR_BINDING,
                metadata={
                    "provider": "google-docs",
                    "endpoint": "drive/v3/files/export",
                    "method": "GET",
                    "connection_id": binding.nango_connection_id,
                },
            )

    async def execute_proxy(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        provider: str,
        method: str,
        endpoint: str,
        data: Any | None = None,
        expected_connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a third-party call for the user's connection via Nango's proxy.

        Called by the internal proxy endpoint (n8n → Sila). Resolves the binding
        server-side; **fails closed** if the connection was revoked (no binding)
        or rotated (the capability was bound to a different connection id).
        """
        spec = self._require_oauth_spec(provider)
        binding = await self._bindings.get(
            user_id=user_id, workspace_id=workspace_id, provider=spec.provider
        )
        if binding is None:
            raise ConnectorError(f"{spec.label} is not connected")
        if expected_connection_id and binding.nango_connection_id != expected_connection_id:
            # The connection the capability was minted for is gone/rotated → fail closed.
            raise ConnectorError(f"{spec.label} connection changed — reconnect required")
        self._require_nango()
        # A Slack post must come from a channel member — self-join first so a freshly
        # connected bot works without a manual invite (channels:join scope).
        if spec.provider == "slack" and endpoint == "chat.postMessage" and isinstance(data, dict):
            await self._slack_ensure_member(
                binding.nango_connection_id,
                spec.nango_integration_id,
                str(data.get("channel") or ""),
            )
        # "Email me the FILE": a mail action whose data carries attach_doc_id gets
        # the user's OWN Google Doc exported as PDF (their google-docs binding,
        # drive.file scope = only docs this app created) and attached. Best-effort:
        # export failure never blocks the mail (the body still carries the link).
        if (
            spec.provider in ("gmail", "outlook-mail")
            and isinstance(data, dict)
            and data.get("attach_doc_id")
        ):
            pdf = await self._export_doc_pdf(
                user_id=user_id, workspace_id=workspace_id, doc_id=str(data["attach_doc_id"])
            )
            if pdf:
                data = {
                    **data,
                    "attachment_b64": base64.b64encode(pdf).decode(),
                    "attachment_name": f"digest-{date.today().isoformat()}.pdf",
                }

        # Lookup PRE-STEP (inverse of a follow-up): some actions need a target id
        # that isn't knowable at build time (To Do's default list) — a GET runs
        # first and its result merges into the action data, server-side.
        pre = pre_step_for(spec.provider, endpoint, data if isinstance(data, dict) else {})
        if pre is not None:
            pre_method, pre_endpoint = pre
            try:
                lookup = await self._nango.proxy(
                    connection_id=binding.nango_connection_id,
                    provider_config_key=spec.nango_integration_id,
                    method=pre_method,
                    endpoint=pre_endpoint,
                    base_url_override=spec.api_base_url,
                )
            except NangoError as err:
                raise ConnectorError(f"Lookup before the action failed: {err}") from err
            data = apply_pre_step(
                spec.provider, endpoint, data if isinstance(data, dict) else {}, lookup
            )

        # Payload seam (WS-9): turn the LOGICAL action + forwarded data into the
        # provider's real endpoint + body (Gmail base64 MIME, Sheets values array,
        # …). The real path — which may embed a resource id — is built here,
        # server-side, from the capability's endpoint; never trusted from the body.
        real_endpoint, real_body = build_payload(spec.provider, endpoint, data)
        # RAW-body actions (OneNote XHTML, OneDrive file content) ride sentinel
        # keys from the builder — never JSON-encoded.
        raw_content: bytes | str | None = None
        raw_content_type: str | None = None
        if isinstance(real_body, dict) and "__content__" in real_body:
            blob = real_body.get("__content__")
            raw_content = blob if isinstance(blob, bytes) else str(blob or "")
            raw_content_type = str(real_body.get("__content_type__") or "text/plain")
            real_body = None
        outcome = Outcome.FAILURE
        try:
            result = await self._nango.proxy(
                connection_id=binding.nango_connection_id,
                provider_config_key=spec.nango_integration_id,
                method=method,
                endpoint=real_endpoint,
                json=real_body,
                content=raw_content,
                content_type=raw_content_type,
                base_url_override=spec.api_base_url,
            )
            # Composite action (WS-9): a second provider call built server-side
            # from the FIRST response — Docs create returns the documentId, then
            # a batchUpdate inserts the generated text as the body (the API has
            # no create-with-content). Still one audited logical action.
            follow = follow_up_payload(spec.provider, endpoint, data, result)
            if follow is not None:
                follow_endpoint, follow_body = follow
                await self._nango.proxy(
                    connection_id=binding.nango_connection_id,
                    provider_config_key=spec.nango_integration_id,
                    method="POST",
                    endpoint=follow_endpoint,
                    json=follow_body,
                    base_url_override=spec.api_base_url,
                )
            outcome = Outcome.SUCCESS
            return result
        except NangoError as err:
            raise ConnectorError(f"Proxied call failed: {err}") from err
        finally:
            # Audit every proxied call (interim replay-detection control until tokens
            # become mint-on-execute). Metadata only — NEVER request/response bodies,
            # and the endpoint is logged path-only (query string stripped) so a future
            # action whose URL carries an id/token can't leak it into the audit.
            await self._audit.record(
                Action.CONNECTOR_PROXY,
                outcome,
                actor_user_id=user_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.CONNECTOR_BINDING,
                metadata={
                    "provider": spec.provider,
                    "endpoint": _audit_path(real_endpoint),
                    "method": method,
                    "connection_id": binding.nango_connection_id,
                },
            )

    async def fetch_access_token(
        self, *, user_id: uuid.UUID, workspace_id: uuid.UUID, provider: str
    ) -> str:
        """The user's access token for ONE in-process request — the escape hatch
        for multipart uploads, which Nango's JSON-only proxy cannot carry (the
        Power BI .pbix import; same limitation that parked the Drive upload).
        Binding-checked and audited like execute_proxy; the token is never
        stored, logged, or returned upstream of the calling service."""
        spec = self._require_oauth_spec(provider)
        binding = await self._bindings.get(
            user_id=user_id, workspace_id=workspace_id, provider=spec.provider
        )
        if binding is None:
            raise ConnectorError(f"{spec.label} is not connected")
        self._require_nango()
        outcome = Outcome.FAILURE
        try:
            token = await self._nango.get_access_token(
                connection_id=binding.nango_connection_id,
                provider_config_key=spec.nango_integration_id,
            )
            outcome = Outcome.SUCCESS
            return token
        except NangoError as err:
            raise ConnectorError(f"Token fetch failed: {err}") from err
        finally:
            await self._audit.record(
                Action.CONNECTOR_PROXY,
                outcome,
                actor_user_id=user_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.CONNECTOR_BINDING,
                metadata={
                    "provider": spec.provider,
                    "endpoint": "credentials:multipart-upload",
                    "method": "TOKEN",
                    "connection_id": binding.nango_connection_id,
                },
            )

    # -- revoke ----------------------------------------------------------

    async def revoke(self, *, user_id: uuid.UUID, workspace_id: uuid.UUID, provider: str) -> None:
        spec = self._require_oauth_spec(provider)
        binding = await self._bindings.get(
            user_id=user_id, workspace_id=workspace_id, provider=spec.provider
        )
        if binding is None:
            return  # already gone — idempotent
        if self._nango.configured:
            try:
                await self._nango.delete_connection(
                    connection_id=binding.nango_connection_id,
                    provider_config_key=spec.nango_integration_id,
                )
            except NangoError:
                # Best-effort upstream delete; we still drop the local binding so
                # execution fails closed (no binding → proxy is unreachable).
                log.warning("connector.nango_revoke_failed", provider=spec.provider)
        await self._bindings.delete(
            user_id=user_id, workspace_id=workspace_id, provider=spec.provider
        )
        await self._audit.record(
            Action.CONNECTOR_REVOKE,
            Outcome.SUCCESS,
            actor_user_id=user_id,
            actor_workspace_id=workspace_id,
            resource_type=ResourceType.CONNECTOR_BINDING,
            metadata={"provider": spec.provider},
        )

    # -- catalog ---------------------------------------------------------

    async def catalog(self, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> list[CatalogEntry]:
        from sila.services.connectors.manifest import CONNECTORS

        bindings = await self._bindings.list_for_user(user_id=user_id, workspace_id=workspace_id)
        by_provider = {b.provider: b for b in bindings}
        entries: list[CatalogEntry] = []
        for spec in CONNECTORS.values():
            if spec.auth_type != "oauth":
                continue
            binding = by_provider.get(spec.provider)
            entries.append(
                CatalogEntry(
                    provider=spec.provider,
                    label=spec.label,
                    auth_type=spec.auth_type,
                    scopes=spec.scopes,
                    connected=binding is not None,
                    selected_resources=dict(binding.selected_resources) if binding else {},
                    resource_pickers=tuple((p.id, p.label) for p in spec.resource_pickers),
                )
            )
        return entries

    # -- helpers ---------------------------------------------------------

    def _require_oauth_spec(self, provider: str) -> ConnectorSpec:
        spec = spec_for(provider)
        if spec is None or spec.auth_type != "oauth":
            raise ConnectorError(f"'{provider}' is not an OAuth connector")
        return spec

    def _require_nango(self) -> None:
        if not self._nango.configured:
            raise ConnectorError("Connector broker is not configured")


def _audit_path(endpoint: str) -> str:
    """The endpoint as logged in the audit: path only, query string stripped, and
    length-capped. Providers can carry identifiers/tokens in the query string;
    they must never land in the audit (the call itself still uses the full URL)."""
    return endpoint.split("?", 1)[0][:200]


def _extract_options(body: dict[str, Any], picker: ResourcePicker) -> list[ResourceOption]:
    """Map a proxied list response to picker options via the manifest paths."""
    items: Any = body
    for key in (p for p in picker.items_path.split(".") if p):
        items = items.get(key, []) if isinstance(items, dict) else []
    if not isinstance(items, list):
        return []
    options: list[ResourceOption] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(picker.value_key)
        label = item.get(picker.label_key) or value
        if value is None:
            continue
        options.append(ResourceOption(value=str(value), label=str(label)))
    return options
