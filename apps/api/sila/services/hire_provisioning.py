"""Hire provisioning — a published workflow agent becomes the HIRER's own copy.

The Bazaar's hire-with-your-own-accounts flow: sanitize the published agent's
recipe (the publisher's targets, KB, and docs NEVER cross the boundary), merge
the hirer's own destinations/documents, rebuild the n8n workflow with
capabilities minted for the HIRER's bindings, register it Draft + Unvetted in
the hirer's workspace, and hand it to the gatekeeper. "Included data" agents
(publish-time `share_docs` opt-in, classification-gated) additionally COPY the
publisher's document bytes into a NEW hirer-owned KB — a copy, never a
cross-tenant reference.

All validation happens BEFORE any side effect (fail-closed): a missing
connector binding, unsupported destination type, or foreign KB returns a 4xx
naming the problem, with nothing created.

Complexity: O(destinations) binding lookups + O(docs) storage round-trips (the
included-data copy; ingestion itself runs async via kb_jobs) + 2 n8n calls.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.n8n.base import N8nService
from sila.core.constants import Action, Outcome, ResourceType
from sila.db.models import Agent, Hire
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.connectors import ConnectorBindingRepository
from sila.db.repositories.hire import HireRepository
from sila.db.repositories.knowledge_base import DocumentRepository, KBRepository
from sila.services import n8n_provisioning
from sila.services.agent_service import AgentService
from sila.services.connectors.manifest import spec_for as connector_spec_for
from sila.services.kb_service import build_kb_service
from sila.services.notification_service import NotificationKind, notify

# v1 hire-setup surface — destination types the setup dialog can collect a
# target for (mirrors DestinationIn's typed fields). Anything else 400s with a
# clear message rather than provisioning an agent that silently can't post.
_REQUIRED_TARGET_FIELD: dict[str, str] = {
    "slack": "channel_id",
    "email": "to",
    "gmail": "to",
    "outlook-mail": "to",
    "google-sheets": "spreadsheet_id",
    "google-chat": "space",
}


async def provision_hire(
    *,
    db: AsyncSession,
    n8n: N8nService,
    agent_service: AgentService,
    source: Agent,
    hirer_user_id: uuid.UUID,
    hirer_workspace_id: uuid.UUID,
    destinations: list[dict[str, Any]],
    schedule: dict[str, Any] | None,
    kb_id: uuid.UUID | None,
    name: str | None,
) -> tuple[Agent, Hire, list[str]]:
    """Provision the hirer's own runnable copy of a published workflow agent.

    Returns (new agent [Draft+Unvetted, NOT yet submitted for review], hire
    bookmark row, warnings). The caller snapshots the response and submits to
    vetting — submit commits the session (the documented ordering gotcha).
    """
    from sila.services.sila.workflow_assembly import build_definition_from_config

    warnings: list[str] = []

    # -- 1. Validate destinations + the hirer's OWN bindings (fail-closed) ----
    bindings = ConnectorBindingRepository(db)
    for dest in destinations:
        dest_type = str(dest.get("type") or "")
        required = _REQUIRED_TARGET_FIELD.get(dest_type)
        if required is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Destination type '{dest_type}' isn't supported for hire setup yet.",
            )
        if not str(dest.get(required) or "").strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"The {dest_type} destination needs '{required}'.",
            )
        spec = connector_spec_for(dest_type)
        if spec is not None and spec.auth_type == "oauth":
            binding = await bindings.get(
                user_id=hirer_user_id, workspace_id=hirer_workspace_id, provider=dest_type
            )
            if binding is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Connect {spec.label} in Connectors before hiring this agent "
                        f"(provider: {dest_type})."
                    ),
                )

    # -- 2. Documents, by the publisher's chosen data mode --------------------
    source_config: dict[str, Any] = dict(source.config or {})
    included_data = bool(source_config.get("share_docs"))
    hirer_kb_id: str | None = None
    hirer_docs: list[str] = []
    if included_data and source_config.get("kb_id"):
        kb_svc = build_kb_service(db)
        new_kb = await kb_svc.create_kb(
            workspace_id=hirer_workspace_id,
            name=f"{source.name} — data"[:100],
            description=f"Documents included with '{source.name}' from the Bazaar.",
            actor_id=hirer_user_id,
        )
        hirer_kb_id = str(new_kb["id"])
        hirer_docs = await kb_svc.copy_documents(
            source_kb_id=uuid.UUID(str(source_config["kb_id"])),
            source_workspace_id=source.workspace_id,
            dest_kb_id=uuid.UUID(hirer_kb_id),
            dest_workspace_id=hirer_workspace_id,
            actor_id=hirer_user_id,
        )
        warnings.append(
            f"{len(hirer_docs)} included document(s) are being processed — the agent "
            "is fully grounded once they finish (a few minutes)."
        )
    elif kb_id is not None:
        kb = await KBRepository(db).get(kb_id, hirer_workspace_id)
        if kb is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Knowledge base not found in your workspace.",
            )
        hirer_kb_id = str(kb_id)
        docs, _total = await DocumentRepository(db).list_by_kb(
            kb_id, hirer_workspace_id, page=1, size=200
        )
        hirer_docs = [d.filename for d in docs]
        if not hirer_docs:
            warnings.append("The selected knowledge base has no documents yet.")
    else:
        warnings.append(
            "No knowledge base attached — the agent will run without your documents. "
            "You can add one later from Knowledge."
        )

    # -- 3. Whitelist config merge (the publisher's targets never cross) ------
    merged: dict[str, Any] = {
        "schedule": schedule or source_config.get("schedule"),
        "destinations": destinations,  # 100% hirer-supplied
        "kb_id": hirer_kb_id,
        "docs": hirer_docs,
        "content": source_config.get("content"),
        "model": source_config.get("model"),
        "language": source_config.get("language"),
        # Provenance — enables update-propagation later; carries no data.
        "source_agent_id": str(source.id),
    }

    # -- 4. Build the definition with the HIRER's identity ---------------------
    final_name = (name or f"{source.name} (from Bazaar)")[:100]
    definition = await build_definition_from_config(
        db=db,
        user_id=hirer_user_id,
        workspace_id=hirer_workspace_id,
        name=final_name,
        config=merged,
    )

    # -- 5. Create the INACTIVE workflow in the hirer's own studio ------------
    session = await n8n_provisioning.ensure_session(
        db=db, n8n=n8n, supabase_user_id=hirer_user_id, sila_full_name=None
    )
    try:
        workflow_id = await n8n.create_workflow(session.cookie_value, definition)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Workflow engine unreachable"
        ) from exc

    # -- 6. Register the Draft+Unvetted copy + the hire bookmark ---------------
    agent = await agent_service.register_workflow_agent(
        workspace_id=hirer_workspace_id,
        owner_id=hirer_user_id,
        workflow_id=str(workflow_id),
        definition=definition,
        config=merged,
        brief=_brief_from_config(merged),
    )
    hire = await HireRepository(db).hire(
        workspace_id=hirer_workspace_id, agent_id=source.id, hired_by=hirer_user_id
    )
    await AuditRepository(db).record(
        action=Action.AGENT_HIRE,
        outcome=Outcome.SUCCESS,
        actor_user_id=hirer_user_id,
        actor_workspace_id=hirer_workspace_id,
        resource_type=ResourceType.AGENT,
        resource_id=agent.id,
        metadata={
            "source_agent_id": str(source.id),
            "workflow_id": str(workflow_id),
            "hire_id": str(hire.id),
            "data_mode": "included" if included_data else "own_docs",
        },
    )
    await notify(
        db,
        user_id=hirer_user_id,
        workspace_id=hirer_workspace_id,
        kind=NotificationKind.AGENT_HIRED,
        title=f"Agent hired: {source.name}",
        body="Your own copy is set up and waiting for its security review.",
        link="/bazaar/hired",
    )
    return agent, hire, warnings


def _brief_from_config(config: dict[str, Any]) -> str:
    """Plain-language one-liner for the agent card (same shape the register
    endpoint builds — see agents.py:_brief_from_config, kept in sync)."""
    from sila.services.sila.workflow_assembly import destination_label

    content = str(config.get("content") or "Summarizes your documents")
    dests = config.get("destinations") or []
    label = " and ".join(destination_label(d) for d in dests)
    return f"{content} — posts to {label}."[:500] if dests else content[:500]
