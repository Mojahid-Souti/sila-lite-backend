"""Email-send capability tokens.

A built n8n workflow with an email destination must ask Sila to send the summary
WITHOUT holding SMTP credentials. So the workflow carries an **email capability**:
an HMAC-signed token bound to exactly (workspace, user, recipient). It is NOT a
bearer secret — it can only trigger a send to that ONE recipient (the workflow body
can't widen it to another address), and it dies the moment the platform secret rotates.

Mirrors `connectors/capability.py` + `run_capability.py` (same HMAC scheme, different
scope). n8n holds only this token; Sila holds the SMTP creds + does the send.

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

_SCOPE = "email_send"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(settings.sila_secret_key.encode(), payload, sha256).digest())


@dataclass(frozen=True)
class EmailCapability:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    to: str  # the ONE recipient this token may send to


def mint(*, workspace_id: uuid.UUID, user_id: uuid.UUID, to: str) -> str:
    """Mint a capability bound to one (workspace, user, recipient)."""
    body = json.dumps(
        {"scope": _SCOPE, "ws": str(workspace_id), "uid": str(user_id), "to": to},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{_b64(body)}.{_sign(body)}"


def verify(token: str) -> EmailCapability | None:
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
        return EmailCapability(
            workspace_id=uuid.UUID(data["ws"]),
            user_id=uuid.UUID(data["uid"]),
            to=str(data["to"]),
        )
    except (KeyError, ValueError):
        return None
