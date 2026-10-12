"""Nango REST client (WS-9) — Auth + Proxy only.

Talks to self-hosted Nango with our secret key (server-side only — the key never
reaches the browser or n8n). Three operations:

- `create_connect_session` → a short-lived token the frontend hands to the
  Connect UI so the user can authorize a provider.
- `proxy` → call a third-party API for a connection; Nango injects the real
  token (we never see it).
- `delete_connection` → revoke (used by the binding-revoke path).

The client never logs request/response bodies (proxied results may contain user
data; the token is never in them but treat the channel as sensitive anyway).

Complexity: each method is O(1) — a single HTTP round-trip.

NOTE: the exact session/proxy/connection paths + the Connect-session response
shape can shift between Nango versions — confirm against the pinned image. The
shapes below match Nango self-host (GitHub master, mid-2026).
"""

from __future__ import annotations

import json as _json
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

_TIMEOUT_S = 15.0


# A provider error CODE is an enum from the API's own vocabulary, never user
# content, so it is always safe to carry. The MESSAGE is not: Graph echoes the
# offending value back ("Recipient 'Ada Lovelace' is not resolved"), which is
# the user's data. But for auth and permission failures the message names the
# missing SCOPE and nothing else, and that is the one case where withholding it
# costs hours — a Teams channel listing 403'd for a year's worth of debugging
# time with "Nango returned 403" as the only clue, while the body said exactly
# which scope was absent. So: code always, message only for these codes.
_CONFIG_ERROR_CODES = frozenset(
    {
        "forbidden",
        "unauthorized",
        "accessdenied",
        "erroraccessdenied",
        "invalidauthenticationtoken",
        "authenticationerror",
        "insufficient_scope",
        "missing_scope",
    }
)


def _provider_error(body: bytes) -> tuple[str, str]:
    """(code, safe detail) pulled out of a provider error body.

    Returns empty strings when the body is not a recognisable error envelope —
    the caller then reports the status alone, exactly as before.
    """
    try:
        payload = _json.loads(body)
    except (ValueError, TypeError):
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    # Graph/Google: {"error": {"code": ..., "message": ...}}. Some APIs put a
    # bare string in "error"; Slack uses {"ok": false, "error": "..."}.
    err = payload.get("error")
    if isinstance(err, str):
        return err[:80], ""
    if not isinstance(err, dict):
        return "", ""
    code = str(err.get("code") or err.get("status") or "")[:80]
    message = ""
    if code.replace(".", "").replace("_", "").lower() in _CONFIG_ERROR_CODES:
        message = str(err.get("message") or "")[:400]
    return code, message


def _error_text(status: int, body: bytes) -> str:
    code, message = _provider_error(body)
    if code and message:
        return f"Nango returned {status} ({code}): {message}"
    if code:
        return f"Nango returned {status} ({code})"
    return f"Nango returned {status}"


class NangoError(RuntimeError):
    """A Nango call failed (network, non-2xx, or an unexpected body shape)."""


class NangoClient:
    def __init__(self, base_url: str, secret_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret_key = secret_key

    @property
    def configured(self) -> bool:
        """False when no secret key is set — callers fail closed (503)."""
        return bool(self._secret_key)

    def _client(self) -> httpx.AsyncClient:
        """The HTTP client factory. Tests override this with a MockTransport."""
        return httpx.AsyncClient(timeout=_TIMEOUT_S)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._secret_key}"}
        if extra:
            headers.update(extra)
        return headers

    async def create_connect_session(
        self,
        *,
        end_user_id: str,
        end_user_email: str | None,
        organization_id: str | None,
        integration_ids: list[str],
    ) -> str:
        """Mint a 30-min Connect session token for one user + allowed integrations.

        POST /connect/sessions. Returns the session token (NOT a provider token).
        """
        end_user: dict[str, Any] = {"id": end_user_id}
        if end_user_email:
            end_user["email"] = end_user_email
        body: dict[str, Any] = {
            "end_user": end_user,
            "allowed_integrations": integration_ids,
        }
        if organization_id:
            body["organization"] = {"id": organization_id}

        data = await self._request("POST", "/connect/sessions", json=body)
        token = _dig(data, "data", "token") or data.get("token")
        if not isinstance(token, str) or not token:
            raise NangoError("Nango connect-session response had no token")
        return token

    async def proxy(
        self,
        *,
        connection_id: str,
        provider_config_key: str,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        content: bytes | str | None = None,
        content_type: str | None = None,
        base_url_override: str | None = None,
    ) -> dict[str, Any]:
        """Call a third-party API for `connection_id` via Nango's proxy.

        Nango injects the credential; we pass only the connection reference.
        `base_url_override` targets a per-product API host when the integration
        lives under a generic provider template (Nango's Base-Url-Override).
        `content`+`content_type` send a RAW request body (OneNote XHTML, OneDrive
        file bytes) instead of JSON — the response is still parsed as JSON.
        """
        path = "/proxy/" + endpoint.lstrip("/")
        extra = {"Connection-Id": connection_id, "Provider-Config-Key": provider_config_key}
        if base_url_override:
            extra["Base-Url-Override"] = base_url_override
        if content_type:
            extra["Content-Type"] = content_type
        headers = self._headers(extra)
        return await self._request(
            method.upper(), path, params=params, json=json, content=content, headers=headers
        )

    async def proxy_raw(
        self,
        *,
        connection_id: str,
        provider_config_key: str,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        base_url_override: str | None = None,
    ) -> bytes:
        """Proxy a call whose RESPONSE is raw bytes (e.g. a Drive PDF export) —
        the JSON `proxy` would choke on a non-JSON body. Same auth/headers."""
        path = "/proxy/" + endpoint.lstrip("/")
        extra = {"Connection-Id": connection_id, "Provider-Config-Key": provider_config_key}
        if base_url_override:
            extra["Base-Url-Override"] = base_url_override
        url = f"{self._base_url}{path}"
        try:
            async with self._client() as client:
                resp = await client.request(
                    method.upper(), url, params=params, headers=self._headers(extra)
                )
        except httpx.RequestError as err:
            log.warning("nango.request_error", method=method, path=path, error=type(err).__name__)
            raise NangoError(f"Nango unreachable: {type(err).__name__}") from err
        if resp.status_code >= 400:
            detail = _error_text(resp.status_code, resp.content)
            log.warning(
                "nango.http_error",
                method=method,
                path=path,
                status=resp.status_code,
                detail=detail,
            )
            raise NangoError(detail)
        return resp.content

    async def get_access_token(self, *, connection_id: str, provider_config_key: str) -> str:
        """The connection's CURRENT access token (Nango refreshes stale ones on
        read). Escape hatch for the rare call Nango's JSON-only proxy cannot
        carry (multipart file uploads, e.g. the Power BI .pbix import) — the
        token is used in-process for one request and never stored or logged."""
        data = await self._request(
            "GET",
            f"/connection/{connection_id}",
            params={"provider_config_key": provider_config_key},
        )
        token = str(_dig(data, "credentials", "access_token") or "")
        if not token:
            raise NangoError("connection returned no access token")
        return token

    async def delete_connection(self, *, connection_id: str, provider_config_key: str) -> None:
        """Revoke + delete a connection. Idempotent — a 404 is treated as gone."""
        try:
            await self._request(
                "DELETE",
                f"/connection/{connection_id}",
                params={"provider_config_key": provider_config_key},
            )
        except NangoError as err:
            if "404" in str(err):
                return
            raise

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        content: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            async with self._client() as client:
                resp = await client.request(
                    method,
                    url,
                    params=params,
                    json=json if content is None else None,
                    content=content,
                    headers=headers or self._headers(),
                )
        except httpx.RequestError as err:
            log.warning("nango.request_error", method=method, path=path, error=type(err).__name__)
            raise NangoError(f"Nango unreachable: {type(err).__name__}") from err
        if resp.status_code >= 400:
            # The provider's error code (and, for permission failures, the scope
            # it names) is what makes a 4xx actionable — see _provider_error.
            detail = _error_text(resp.status_code, resp.content)
            log.warning(
                "nango.http_error",
                method=method,
                path=path,
                status=resp.status_code,
                detail=detail,
            )
            raise NangoError(detail)
        if not resp.content:
            return {}
        try:
            body = resp.json()
        except ValueError as err:
            raise NangoError("Nango returned a non-JSON body") from err
        return body if isinstance(body, dict) else {"data": body}


def _dig(data: dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur
