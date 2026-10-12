"""Build an executable n8n workflow definition FROM a publisher_config.

This is the single workflow-shape source of truth shared by the build path and the
safe-fields edit path (Prompt 2): given an agent's config (schedule, destinations,
kb_id, model, content), it mints the capability tokens and assembles the same
trigger → Generate-summary(Sila) → Slack/email(Sila) chain `build_rag_workflow`
produces. The edit path calls this to rebuild the live workflow when the user changes
schedule/destination — never a hand-edit of n8n.

n8n holds no secret: Slack posts through the Nango proxy (capability token), email
through the platform SMTP endpoint (capability token). A Slack destination needs a
live connection; if the binding is gone the node is dropped (fail-closed) and the
caller surfaces "reconnect Slack".

Complexity: O(destinations) capability mints; one binding lookup per Slack dest.
"""

from __future__ import annotations

import uuid
from typing import Any

from sila.core.config import settings
from sila.db.repositories.connectors import ConnectorBindingRepository
from sila.services.connectors import capability
from sila.services.connectors.manifest import spec_for as connector_spec_for
from sila.services.n8n_workflow_builder import ConnectorNode, WorkflowSpec, build_rag_workflow
from sila.services.sila import email_capability, run_capability

# The output language is ALWAYS stated explicitly — a small model otherwise picks
# one at random (an English doc + English chat once produced an Arabic digest).
_LANGUAGE_DIRECTIVE = {"ar": "Write in Arabic.", "en": "Write in English."}


def summary_instruction_for(content: str, language: str | None = None) -> str:
    """The generation instruction a publisher bakes into its workflow (and its
    dry-run preview). `language` comes from the user's conversation, detected
    deterministically (build_slots.detect_goal_language)."""
    base = (
        f"{content}. Write it as 3-5 concise, professional bullet points."
        if content
        else "Summarize the content into 3-5 concise, professional bullet points."
    )
    directive = _LANGUAGE_DIRECTIVE.get(language or "")
    return f"{base} {directive}" if directive else base


def destination_label(dest: dict[str, Any]) -> str:
    """The human label for one publisher_config destination — the agent card's and
    the brief's 'posts to …' text. Email and Gmail both carry the recipient in `to`;
    other connector types fall back to their manifest label (Google Chat, …)."""
    dtype = str(dest.get("type") or "")
    if dtype == "slack":
        return f"#{dest.get('channel_name') or dest.get('channel_id') or 'channel'}"
    if dtype == "google-sheets":
        sid = str(dest.get("spreadsheet_id") or "")
        return f"Google Sheet {sid[:12]}…" if len(sid) > 12 else f"Google Sheet {sid}"
    if dest.get("to"):
        return str(dest["to"])
    cspec = connector_spec_for(dtype)
    return cspec.label if cspec else (dtype or "email")


async def missing_oauth_bindings(
    *,
    db: Any,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    destinations: list[dict[str, Any]],
) -> list[str]:
    """Provider labels among `destinations` whose OAuth binding is MISSING for
    `user_id`. Callers use this to fail closed with a clear 400 instead of the
    assembly's silent node drop (team-sharing edits re-mint as the agent's
    OWNER — a gone owner connection must be named, not swallowed).
    O(destinations) binding lookups."""
    bindings = ConnectorBindingRepository(db)
    missing: list[str] = []
    for dest in destinations:
        dtype = str(dest.get("type") or "")
        cspec = connector_spec_for(dtype)
        if cspec is None or cspec.auth_type != "oauth" or cspec.action is None:
            continue
        binding = await bindings.get(user_id=user_id, workspace_id=workspace_id, provider=dtype)
        if binding is None and cspec.label not in missing:
            missing.append(cspec.label)
    return missing


async def build_definition_from_config(
    *,
    db: Any,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    name: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the n8n definition for `config`. Mirrors create_agent_workflow's spec."""
    kb_id = config.get("kb_id")
    model = str(config.get("model") or settings.sila_llm_model)
    schedule = config.get("schedule") or {}
    schedule_cron = schedule.get("cron")
    content = str(config.get("content") or "").strip()
    destinations = config.get("destinations") or []

    run_url: str | None = None
    run_token: str | None = None
    if kb_id:
        run_url = f"{settings.sila_api_internal_url}/v1/internal/agents/run"
        run_token = run_capability.mint(
            workspace_id=workspace_id, kb_id=uuid.UUID(str(kb_id)), model=model
        )

    integrations: list[str] = []
    connectors: dict[str, ConnectorNode] = {}
    connector_proxy_url: str | None = None
    email_url: str | None = None
    email_token: str | None = None
    bindings = ConnectorBindingRepository(db)

    for dest in destinations:
        dtype = str(dest.get("type") or "")
        cspec = connector_spec_for(dtype)
        if cspec is not None and cspec.auth_type == "oauth":
            binding = await bindings.get(user_id=user_id, workspace_id=workspace_id, provider=dtype)
            if binding is None or cspec.action is None:
                continue  # not connected / no action → drop (fail-closed)
            action = cspec.action
            data: dict[str, Any] = dict(action.static)
            if action.resource_field:
                # Picker-chosen resource, stored on the destination (Slack's channel_id).
                data[action.resource_field] = str(dest.get("channel_id") or "")
            if action.slot_arg and action.slot_field and dest.get(action.slot_field):
                # Interview-captured target, stored under the payload field itself
                # (gmail "to", sheets "spreadsheet_id") — see _create_agent_workflow.
                data[action.slot_field] = str(dest[action.slot_field])
            integrations.append(dtype)
            connectors[dtype] = ConnectorNode(
                provider=dtype,
                endpoint=action.endpoint,
                method=action.method,
                data=data,
                token=capability.mint(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    provider=dtype,
                    endpoint=action.endpoint,
                    connection_id=binding.nango_connection_id,
                ),
                message_field=action.message_field,
            )
            connector_proxy_url = f"{settings.sila_api_internal_url}/v1/internal/connectors/proxy"
        elif dtype == "email" and dest.get("to"):
            integrations.append("email")
            email_url = f"{settings.sila_api_internal_url}/v1/internal/agents/send-email"
            email_token = email_capability.mint(
                workspace_id=workspace_id, user_id=user_id, to=str(dest["to"])
            )

    summary_instruction = summary_instruction_for(
        content, str(config.get("language") or "") or None
    )
    spec = WorkflowSpec(
        name=name,
        pre_prompt=f"You are {name}, a helpful assistant for this organization.",
        model=model,
        ollama_url=settings.ollama_url,
        run_url=run_url,
        run_token=run_token,
        summary_instruction=summary_instruction,
        email_url=email_url,
        email_token=email_token,
        schedule_cron=schedule_cron,
        integrations=integrations,
        connectors=connectors,
        connector_proxy_url=connector_proxy_url,
    )
    return build_rag_workflow(spec)
