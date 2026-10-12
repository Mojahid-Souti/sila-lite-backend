"""Connector proxy capability tokens (WS-9).

The execution path is n8n → Sila → Nango. The n8n workflow must authenticate to
Sila's internal proxy, but we don't want a *reusable* secret sitting in n8n. So
each built workflow carries a **capability token**: an HMAC-signed, single-purpose
reference that encodes exactly (workspace_id, user_id, provider). It is NOT a
bearer secret — it can only trigger a proxied call for that one connection, and
it dies the moment the binding is revoked (the proxy then fails closed).

The token never carries the third-party credential; that stays in Nango.

Complexity: O(1) sign/verify (one HMAC).
"""

from __future__ import annotations

import base64
import hmac
import json
import uuid
from dataclasses import dataclass
from hashlib import sha256

from sila.core.config import settings

_SCOPE = "connector_proxy"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> str:
    sig = hmac.new(settings.sila_secret_key.encode(), payload, sha256).digest()
    return _b64(sig)


@dataclass(frozen=True)
class Capability:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    provider: str
    endpoint: str  # the ONE third-party endpoint this token may call
    connection_id: str  # the ONE connection it may act on


def mint(
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    provider: str,
    endpoint: str,
    connection_id: str,
) -> str:
    """Mint a capability bound to (provider + endpoint + connection).

    It is NOT a provider-wide bearer: it authorizes exactly one action endpoint
    on exactly one connection. Not single-use and not time-bound (a baked
    workflow runs repeatedly and lives indefinitely), so the controls are:
    HMAC-unforgeable, narrowly scoped, and revoked the moment the binding (or its
    connection) changes — the proxy then fails closed.
    """
    body = json.dumps(
        {
            "scope": _SCOPE,
            "ws": str(workspace_id),
            "uid": str(user_id),
            "provider": provider,
            "endpoint": endpoint,
            "conn": connection_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{_b64(body)}.{_sign(body)}"


def verify(token: str) -> Capability | None:
    """Verify + decode a token, or None if malformed / tampered. Timing-safe."""
    try:
        body_b64, sig = token.split(".", 1)
    except ValueError:
        return None
    body = _unb64(body_b64)
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if data.get("scope") != _SCOPE:
        return None
    try:
        return Capability(
            workspace_id=uuid.UUID(data["ws"]),
            user_id=uuid.UUID(data["uid"]),
            provider=str(data["provider"]),
            endpoint=str(data["endpoint"]),
            connection_id=str(data["conn"]),
        )
    except (KeyError, ValueError):
        return None
