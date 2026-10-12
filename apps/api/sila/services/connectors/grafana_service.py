"""Grafana connector service — register a workspace's Grafana, create dashboards.

Two operations, both admin-gated at the API layer:
  - save_connection: validate + health-check the URL, encrypt the token, store one
    row per workspace.
  - create_dashboard: fetch the connection, decrypt the token, build the dashboard
    JSON from a spec, POST it, return the URL.

URL safety: a self-hosted Grafana normally lives on the INTERNAL network
(localhost / docker / private LAN), so the standard SSRF guard (which blocks all
private ranges) can't be used verbatim — it would reject every legitimate
Grafana. Instead we block the genuinely dangerous targets (cloud-metadata +
link-local) and allow private LAN. Registration is admin-only and the admin
supplies their own token, so the residual pivot risk is low.

Complexity: O(panels) to build; one HTTP round-trip to create.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.connectors.grafana_client import DashboardResult, GrafanaClient, GrafanaError
from sila.core.security.field_crypto import decrypt_field, encrypt_field
from sila.db.repositories.grafana import GrafanaConnectionRepository
from sila.services.connectors.grafana_dashboard import build_dashboard, build_dashboard_with_data

_ALLOWED_SCHEMES = {"http", "https"}
# Always-blocked hosts/addresses — the real SSRF danger (cloud credential theft).
_BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata"}


class GrafanaConfigError(Exception):
    """Registration input was invalid (bad URL, unreachable, bad token)."""


def _validate_url(url: str) -> str:
    """Normalize + gate a Grafana base URL. Allows private LAN (on-prem Grafana)
    but blocks cloud-metadata / link-local. Raises GrafanaConfigError on reject."""
    u = (url or "").strip().rstrip("/")
    parts = urlsplit(u)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise GrafanaConfigError("The Grafana URL must start with http:// or https://.")
    host = parts.hostname
    if not host:
        raise GrafanaConfigError("The Grafana URL has no host.")
    if host.lower() in _BLOCKED_HOSTS:
        raise GrafanaConfigError("That address is not allowed.")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise GrafanaConfigError("That address is not allowed.")
    except ValueError:
        pass  # a hostname, not a literal IP — fine
    return u


class GrafanaService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = GrafanaConnectionRepository(db)
        # Set by create_dashboard: the datasource it auto-attached (None if the
        # caller named one or none exist) — the tool reports it to the user.
        self.last_datasource_name: str | None = None
        # Set by create_dashboard_with_data: the no-sign-in public share URL
        # (None when public sharing is unavailable — tool falls back).
        self.last_public_url: str | None = None

    async def save_connection(
        self, *, workspace_id: uuid.UUID, base_url: str, token: str, actor_id: uuid.UUID
    ) -> None:
        """Validate the URL, verify Grafana answers + the token works, then store it
        (token Fernet-encrypted). Raises GrafanaConfigError on any failure."""
        url = _validate_url(base_url)
        tok = (token or "").strip()
        if not tok:
            raise GrafanaConfigError("An API token is required.")
        client = GrafanaClient(url, tok)
        if not await client.verify():
            raise GrafanaConfigError(
                "Couldn't connect — check the URL and that the API token is valid "
                "(Editor role). For Grafana Cloud, use your https://<org>.grafana.net URL."
            )
        await self._repo.upsert(
            workspace_id=workspace_id,
            base_url=url,
            token_encrypted=encrypt_field(tok),
            registered_by=actor_id,
        )

    async def connect_builtin(self, *, workspace_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        """One-click connect to Sila's BUILT-IN Grafana (the sila-grafana
        container): the backend mints a service-account token itself using the
        Grafana admin credentials from env — the user never opens Grafana.
        Raises GrafanaConfigError when the container/env isn't available."""
        import httpx

        from sila.core.config import settings

        if not settings.grafana_admin_password:
            raise GrafanaConfigError(
                "The built-in Grafana isn't configured (GRAFANA_ADMIN_PASSWORD is unset)."
            )
        base = settings.grafana_internal_url.rstrip("/")
        auth = ("admin", settings.grafana_admin_password)
        sa_name = f"sila-ws-{str(workspace_id)[:8]}"
        try:
            async with httpx.AsyncClient(timeout=15.0, auth=auth) as client:
                created = await client.post(
                    f"{base}/api/serviceaccounts", json={"name": sa_name, "role": "Admin"}
                )
                if created.status_code < 400:
                    sa_id = int(created.json()["id"])
                else:
                    # Reconnect: the account exists — find it and mint a fresh token.
                    found = await client.get(
                        f"{base}/api/serviceaccounts/search", params={"query": sa_name}
                    )
                    accounts = found.json().get("serviceAccounts", [])
                    match = next((a for a in accounts if a.get("name") == sa_name), None)
                    if match is None:
                        raise GrafanaConfigError("Couldn't create the Grafana service account.")
                    sa_id = int(match["id"])
                token_resp = await client.post(
                    f"{base}/api/serviceaccounts/{sa_id}/tokens",
                    json={"name": f"sila-{uuid.uuid4().hex[:8]}"},
                )
                if token_resp.status_code >= 400:
                    raise GrafanaConfigError("Couldn't create the Grafana API token.")
                token = str(token_resp.json().get("key", ""))
        except httpx.HTTPError as exc:
            raise GrafanaConfigError(
                "The built-in Grafana isn't reachable — is the sila-grafana "
                "container running? (make up / docker compose up -d sila-grafana)"
            ) from exc
        await self.save_connection(
            workspace_id=workspace_id, base_url=base, token=token, actor_id=actor_id
        )

    @staticmethod
    def to_public_url(base_url: str, url: str | None) -> str | None:
        """Rewrite an internal built-in-Grafana link (http://sila-grafana:3000/…)
        to the browser-reachable public base — users can't open Docker-network
        hostnames. Non-built-in URLs pass through unchanged."""
        from sila.core.config import settings

        internal = settings.grafana_internal_url.rstrip("/")
        if url and base_url.rstrip("/") == internal:
            return url.replace(internal, settings.grafana_public_url.rstrip("/"), 1)
        return url

    async def get_status(self, workspace_id: uuid.UUID) -> dict[str, object] | None:
        """The connection's public shape (URL only, NEVER the token), or None."""
        conn = await self._repo.get_for_workspace(workspace_id)
        if conn is None:
            return None
        return {"base_url": conn.base_url, "connected": True}

    async def create_dashboard(
        self,
        *,
        workspace_id: uuid.UUID,
        title: str,
        panels: list[dict[str, object]],
        datasource_uid: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> DashboardResult:
        """Build the dashboard JSON from the spec and create it on the workspace's
        Grafana. Raises GrafanaConfigError if no Grafana is connected, ValueError on
        a bad spec, GrafanaError on an API failure. `progress` (optional) receives a
        note at each REAL stage boundary — the live trace shows it."""

        def _note(text: str) -> None:
            if progress is not None:
                progress(text)

        conn = await self._repo.get_for_workspace(workspace_id)
        if conn is None:
            raise GrafanaConfigError("No Grafana is connected for this workspace yet.")
        token = decrypt_field(conn.token_encrypted)
        client = GrafanaClient(conn.base_url, token)

        # No datasource named -> attach the instance's default (else the panels
        # render "No data" with their queries dangling). Best-effort: an empty
        # list just builds the dashboard unattached.
        ds_type: str | None = None
        ds_name: str | None = None
        if datasource_uid is None:
            _note("Finding your default datasource…")
            sources = await client.list_datasources()
            chosen = next((d for d in sources if d.is_default), sources[0] if sources else None)
            if chosen is not None:
                datasource_uid, ds_type, ds_name = chosen.uid, chosen.type, chosen.name

        _note(f"Designing the layout — {len(panels)} panel(s)…")
        dashboard = build_dashboard(
            title, panels, datasource_uid=datasource_uid, datasource_type=ds_type
        )
        _note("Creating it on your Grafana…")
        result = await client.create_dashboard(dashboard)
        self.last_datasource_name = ds_name
        return DashboardResult(
            uid=result.uid,
            url=self.to_public_url(conn.base_url, result.url) or result.url,
        )

    async def create_dashboard_with_data(
        self,
        *,
        workspace_id: uuid.UUID,
        title: str,
        charts: list[dict[str, object]],
        progress: Callable[[str], None] | None = None,
        uid: str | None = None,
    ) -> DashboardResult:
        """Create a dashboard whose panels EMBED the given chart data (TestData
        csv_content) — real charts from an uploaded file, no metrics pipeline.
        Pass `uid` (a previous result's uid) to UPDATE that dashboard in place —
        the chat-editing path ("add a chart to the same dashboard").
        Raises GrafanaConfigError when no Grafana is connected or no TestData
        datasource exists and the token can't create one (needs Admin once)."""

        def _note(text: str) -> None:
            if progress is not None:
                progress(text)

        conn = await self._repo.get_for_workspace(workspace_id)
        if conn is None:
            raise GrafanaConfigError("No Grafana is connected for this workspace yet.")
        client = GrafanaClient(conn.base_url, decrypt_field(conn.token_encrypted))
        _note("Preparing the data-carrier datasource…")
        ds_uid = await client.find_or_create_testdata()
        if not ds_uid:
            # Editor tokens can create neither datasources nor snapshots (probed
            # live on Grafana 11) — the ONE reliable unlock is an Admin token or a
            # manually-added TestData datasource. Say exactly that.
            raise GrafanaConfigError(
                "Embedding file data needs a 'TestData DB' datasource and this API "
                "token can't create one. Two 30-second fixes in Grafana: set the "
                "sila service account's role to Admin (Administration → Service "
                "accounts), OR add the datasource yourself (Connections → Data "
                "sources → Add → TestData DB). Then ask me again."
            )
        _note(f"Embedding the data — {len(charts)} panel(s)…")
        dashboard = build_dashboard_with_data(title, charts, ds_uid)
        if uid:
            dashboard["uid"] = uid  # same dashboard, updated in place
        _note("Creating it on your Grafana…")
        result = await client.create_dashboard(dashboard, overwrite=bool(uid))
        # The guaranteed-viewable link: enable PUBLIC sharing so workspace users
        # open the dashboard with zero sign-in. Best-effort — a failed share step
        # never blocks the build (the tool falls back to the internal URL).
        _note("Publishing the share link…")
        self.last_public_url = self.to_public_url(
            conn.base_url, await client.ensure_public_dashboard(result.uid)
        )
        return DashboardResult(
            uid=result.uid,
            url=self.to_public_url(conn.base_url, result.url) or result.url,
        )


__all__ = ["GrafanaService", "GrafanaConfigError", "GrafanaError"]
