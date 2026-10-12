"""Agent-run capability tokens.

A built n8n workflow must ask Sila to run the agent's RAG (KB retrieval + LLM
summary) without holding a JWT or a reusable secret. So the workflow carries a
**run capability**: an HMAC-signed, single-purpose token that encodes exactly
(workspace_id, kb_id, model). It is NOT a bearer secret — it can only trigger the
agent-run for that one KB, and it dies the moment the platform secret rotates.

Mirrors `services/connectors/capability.py` (same HMAC scheme, different scope).
n8n holds only this token; Sila does the retrieval + generation server-side with
the service role, authorized solely by the token's scope.

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

_SCOPE = "agent_run"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(settings.sila_secret_key.encode(), payload, sha256).digest())


@dataclass(frozen=True)
class RunCapability:
    workspace_id: uuid.UUID
    kb_id: uuid.UUID
    model: str


def mint(*, workspace_id: uuid.UUID, kb_id: uuid.UUID, model: str) -> str:
    """Mint a run capability bound to one (workspace, kb, model). HMAC-unforgeable,
    narrowly scoped, not time-bound (a baked workflow runs repeatedly)."""
    body = json.dumps(
        {"scope": _SCOPE, "ws": str(workspace_id), "kb": str(kb_id), "model": model},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{_b64(body)}.{_sign(body)}"


def verify(token: str) -> RunCapability | None:
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
        return RunCapability(
            workspace_id=uuid.UUID(data["ws"]),
            kb_id=uuid.UUID(data["kb"]),
            model=str(data["model"]),
        )
    except (KeyError, ValueError):
        return None
