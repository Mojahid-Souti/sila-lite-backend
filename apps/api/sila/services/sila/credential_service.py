"""SILA credential service — collect a secret mid-chat ("Incognito") and put it in an
ENCRYPTED home, returning only a non-secret reference.

Two targets:
  - n8n: create a credential in the user's own n8n account (n8n encrypts it at rest with
    N8N_ENCRYPTION_KEY) and return (n8n_type, cred_id) to wire into the built workflow's
    integration node.
  - mcp: register an MCP server with the token (Fernet-encrypted) via ToolService.

The raw `values` flow ONLY through `connect()` into that encrypted store and are then
dropped — never persisted as a sila_message, never put in the LLM context, never logged.

Complexity: O(1) per credential.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from sila.adapters.auth.base import UserResult
from sila.adapters.n8n.rest import N8nRestAdapter
from sila.core.config import settings
from sila.services import n8n_provisioning

log = structlog.get_logger("sila.credentials")


@dataclass(frozen=True)
class CredentialField:
    key: str
    label: str
    secret: bool = False


@dataclass(frozen=True)
class CredentialSpec:
    provider: str
    label: str
    target: str  # "n8n" | "mcp"
    fields: tuple[CredentialField, ...]
    n8n_type: str | None = None  # n8n credential type (target="n8n")
    admin_only: bool = False


# Curated providers only — SILA can't invent credential types.
_SPECS: dict[str, CredentialSpec] = {
    "slack": CredentialSpec(
        provider="slack",
        label="Slack",
        target="n8n",
        n8n_type="slackApi",
        fields=(CredentialField("accessToken", "Slack bot token", secret=True),),
    ),
    "email": CredentialSpec(
        provider="email",
        label="Email (SMTP)",
        target="n8n",
        n8n_type="smtp",
        fields=(
            CredentialField("host", "SMTP host"),
            CredentialField("port", "Port"),
            CredentialField("user", "Username"),
            CredentialField("password", "Password", secret=True),
        ),
    ),
    "mcp": CredentialSpec(
        provider="mcp",
        label="External tool (MCP)",
        target="mcp",
        admin_only=True,  # registering a tool server is an admin action
        fields=(
            CredentialField("url", "Server URL"),
            CredentialField("token", "API key / token", secret=True),
        ),
    ),
}


def spec_for(provider: str) -> CredentialSpec | None:
    return _SPECS.get((provider or "").lower())


def fields_payload(spec: CredentialSpec) -> list[dict[str, Any]]:
    """The field descriptors sent to the Incognito panel (no values)."""
    return [{"key": f.key, "label": f.label, "secret": f.secret} for f in spec.fields]


@dataclass(frozen=True)
class ConnectResult:
    label: str
    # An n8n node credential to wire into the workflow: (n8n_type, cred_id). None for MCP.
    wiring: tuple[str, str] | None = None


async def connect(
    *,
    provider: str,
    values: dict[str, str],
    user: UserResult,
    workspace_id: uuid.UUID,
    db: Any,  # AsyncSession
) -> ConnectResult | None:
    """Create the credential in its encrypted home; return a non-secret result.
    NEVER logs `values`. Returns None for an unknown provider."""
    spec = spec_for(provider)
    if spec is None:
        log.warning("sila.credentials.unknown_provider", provider=provider)
        return None

    if spec.target == "n8n" and spec.n8n_type:
        n8n = N8nRestAdapter(settings.n8n_internal_url)
        session = await n8n_provisioning.ensure_session(
            db=db, n8n=n8n, supabase_user_id=user.id, sila_full_name=None
        )
        data: dict[str, Any] = {}
        for f in spec.fields:
            raw = values.get(f.key, "")
            data[f.key] = int(raw) if f.key == "port" and raw.isdigit() else raw
        cred_id = await n8n.create_n8n_credential(
            session.cookie_value, name=f"{spec.label} (SILA)", type_=spec.n8n_type, data=data
        )
        return ConnectResult(label=spec.label, wiring=(spec.n8n_type, cred_id))

    if spec.target == "mcp":
        from sila.services.tool_service import ToolService

        srv = await ToolService(db).register_mcp_server(
            workspace_id=workspace_id,
            actor_id=user.id,
            name=(values.get("name") or "Connected tool")[:80],
            description="Connected via SILA",
            url=values.get("url", ""),
            auth_token=values.get("token") or None,
        )
        return ConnectResult(label=str(srv.name))

    return None
