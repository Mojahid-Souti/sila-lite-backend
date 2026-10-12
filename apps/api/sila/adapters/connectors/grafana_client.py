"""Grafana HTTP client — create a dashboard on the workspace's own Grafana.

Talks to a user-provided Grafana base URL with a static API token. The URL is
SSRF-validated by the CALLER before construction (so an internal-network pivot is
blocked); this client just POSTs the dashboard JSON and returns the resulting URL.

Complexity: O(1) — one bounded HTTP round-trip.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class GrafanaError(Exception):
    """A Grafana API call failed (network, auth, or a 4xx/5xx)."""


@dataclass(frozen=True)
class DashboardResult:
    uid: str
    url: str  # absolute URL to the created dashboard


@dataclass(frozen=True)
class Datasource:
    uid: str
    name: str
    type: str  # "prometheus", "loki", "postgres", …
    is_default: bool


class GrafanaClient:
    def __init__(self, base_url: str, token: str, *, timeout_s: float = 20.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_s

    async def create_dashboard(
        self, dashboard: dict[str, object], *, overwrite: bool = False
    ) -> DashboardResult:
        """POST /api/dashboards/db (upsert). Returns the new dashboard's uid + URL."""
        body = {"dashboard": dashboard, "overwrite": overwrite, "message": "Created by Sila"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base}/api/dashboards/db",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise GrafanaError(f"Couldn't reach Grafana: {type(exc).__name__}") from exc
        if resp.status_code == 401 or resp.status_code == 403:
            raise GrafanaError("Grafana rejected the API token (check its permissions).")
        if resp.status_code >= 400:
            detail = resp.text[:200]
            raise GrafanaError(f"Grafana returned HTTP {resp.status_code}: {detail}")
        data = resp.json()
        uid = str(data.get("uid", ""))
        url = str(data.get("url", ""))
        full = f"{self._base}{url}" if url.startswith("/") else url
        return DashboardResult(uid=uid, url=full or self._base)

    async def list_datasources(self) -> list[Datasource]:
        """GET /api/datasources — used to attach panels to a REAL datasource when the
        caller didn't name one (otherwise every panel shows 'No data'). Best-effort:
        returns [] on any failure (the dashboard still gets created)."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base}/api/datasources",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            if resp.status_code >= 400:
                return []
            items = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        out: list[Datasource] = []
        for d in items if isinstance(items, list) else []:
            if isinstance(d, dict) and d.get("uid"):
                out.append(
                    Datasource(
                        uid=str(d["uid"]),
                        name=str(d.get("name", "")),
                        type=str(d.get("type", "")),
                        is_default=bool(d.get("isDefault", False)),
                    )
                )
        return out

    async def ensure_public_dashboard(self, uid: str) -> str | None:
        """Enable Grafana's PUBLIC sharing for a dashboard and return the public
        URL (opens with zero sign-in) — the guaranteed-viewable link for workspace
        users. An existing share is found (GET) and reused so the link stays
        stable across rebuilds; only a first build creates one (POST).
        Best-effort: returns None on any failure (feature off, missing permission)
        so a build is never blocked by the share step."""
        path = f"{self._base}/api/dashboards/uid/{uid}/public-dashboards"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # GET first — an existing share keeps ITS link stable across
                # rebuilds (repeat POSTs mint a new token on some versions).
                resp = await client.get(path, headers=headers)
                if resp.status_code >= 400 or not resp.json().get("accessToken"):
                    resp = await client.post(path, json={"isEnabled": True}, headers=headers)
            if resp.status_code >= 400:
                return None
            token = str(resp.json().get("accessToken", ""))
        except (httpx.HTTPError, ValueError):
            return None
        return f"{self._base}/public-dashboards/{token}" if token else None

    async def find_or_create_testdata(self) -> str | None:
        """The uid of a TestData datasource (used to embed CSV data in panels).
        Finds an existing one first (any role can); creating needs Admin — returns
        None when neither works so the caller can explain what to do."""
        for d in await self.list_datasources():
            if d.type == "grafana-testdata-datasource":
                return d.uid
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base}/api/datasources",
                    json={
                        "name": "Sila Data",
                        "type": "grafana-testdata-datasource",
                        "access": "proxy",
                    },
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            if resp.status_code < 400:
                return str(resp.json().get("datasource", {}).get("uid") or "")
        except (httpx.HTTPError, ValueError):
            pass
        return None

    async def verify(self) -> bool:
        """Validate the connection AT registration by hitting an AUTHENTICATED
        endpoint (GET /api/org) — this proves both that Grafana is reachable AND
        that the token works (self-hosted and Grafana Cloud behave identically)."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base}/api/org",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            return resp.status_code < 400
        except httpx.HTTPError:
            return False
