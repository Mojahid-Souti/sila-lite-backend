"""AgentService — business logic for the agent lifecycle.

State machine (enforced here, not in the DB):
  DRAFT → PUBLISHED  (publish)
  DRAFT → ARCHIVED   (archive)
  PUBLISHED → ARCHIVED (archive)
  Any → new DRAFT    (rollback — non-destructive; creates new version)
  ARCHIVED → *       FORBIDDEN

All state-changing methods write an audit log entry as fire-and-forget.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.agent_runtime.base import AgentDefinitionError, AgentRuntime
from sila.adapters.agent_runtime.n8n_workflow import (
    N8nWorkflowRuntime,
    WorkflowNotInvokableError,
)
from sila.core.config import settings
from sila.core.cost_telemetry import cost_metadata, usage_from_dify

if TYPE_CHECKING:
    from sila.services.bazaar_service import BazaarService
from sila.core.constants import (
    Action,
    AgentKind,
    AgentSource,
    AgentStatus,
    Classification,
    Outcome,
    ResourceType,
)
from sila.core.utils.workflow_validator import WORKFLOW_MAX_BYTES, validate_workflow
from sila.core.utils.yaml_validator import validate_yaml
from sila.db.models import Agent
from sila.db.repositories.agent import AgentRepository
from sila.db.repositories.agent_import import AgentImportRepository
from sila.db.repositories.agent_version import AgentVersionRepository
from sila.db.repositories.audit import AuditRepository
from sila.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

_ALLOWED_TRANSITIONS: dict[AgentStatus, set[AgentStatus]] = {
    AgentStatus.DRAFT: {AgentStatus.PUBLISHED, AgentStatus.ARCHIVED},
    AgentStatus.IN_REVIEW: {AgentStatus.PUBLISHED, AgentStatus.ARCHIVED},
    AgentStatus.PUBLISHED: {AgentStatus.ARCHIVED},
    AgentStatus.ARCHIVED: set(),
}


async def _as_stream(result: dict) -> AsyncIterator[dict]:  # type: ignore[type-arg]
    """Adapt a one-shot invoke result to the streaming shape (token, then usage)."""
    yield {"token": str(result.get("answer", ""))}
    yield {"usage": result.get("usage", {})}


def _usage_metadata(
    dify_metadata: dict,  # type: ignore[type-arg]
    *,
    agent_id: object = None,
    surface: str = "sandbox",
) -> dict | None:  # type: ignore[type-arg]
    """Dify usage block -> the canonical cost-telemetry audit metadata.

    Records input/output tokens separately, the wall-clock latency, the agent id
    (so cost can be grouped per agent) and the invocation surface. Dify does not
    pass Ollama's prefill/decode durations through, so `active_seconds` stays 0
    for this engine — the cost query treats that as "GPU timing unavailable"
    rather than "zero GPU time". See core/cost_telemetry.py.
    """
    usage = (dify_metadata or {}).get("usage", {}) or {}
    latency_ms = (
        round(float(usage["latency"]) * 1000, 1) if usage.get("latency") is not None else None
    )
    block = usage_from_dify(dify_metadata)
    # Dify reports a total that may not equal prompt+completion; prefer its own.
    if usage.get("total_tokens") is not None and not (
        block["input_tokens"] or block["output_tokens"]
    ):
        block["output_tokens"] = int(usage["total_tokens"])
    return cost_metadata(block, agent_id=agent_id, latency_ms=latency_ms, surface=surface)


class AgentService:
    def __init__(self, db: AsyncSession, runtime: AgentRuntime) -> None:
        self._db = db
        self._runtime = runtime
        self._workflow_runtime = N8nWorkflowRuntime()
        self._agents = AgentRepository(db)
        self._versions = AgentVersionRepository(db)
        self._imports = AgentImportRepository(db)
        self._audit = AuditRepository(db)

    # ------------------------------------------------------------------
    # Import / Create
    # ------------------------------------------------------------------

    async def import_from_yaml(
        self,
        *,
        workspace_id: uuid.UUID,
        owner_id: uuid.UUID,
        raw_yaml: bytes,
        filename: str,
    ) -> Agent:
        """Parse, validate, and register a Dify DSL YAML upload.

        On success: inserts agent + agent_version + agent_import (success).
        On failure: inserts agent_import (failed) and raises HTTP 422.
        O(s) where s = YAML size ≤ 1 MiB.
        """
        dsl, errors = validate_yaml(
            raw_yaml,
            workspace_tool_ids=frozenset(),  # Phase 2: no tools registered yet
        )

        if errors:
            async with self._db.begin_nested():
                await self._imports.record(
                    workspace_id=workspace_id,
                    imported_by=owner_id,
                    source=AgentSource.YAML_UPLOAD,
                    status="failed",
                    filename=filename,
                    raw_yaml=raw_yaml.decode(errors="replace"),
                    error_msg="; ".join(errors),
                )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"errors": errors},
            )

        app_name = dsl["app"]["name"]
        app_description = dsl.get("app", {}).get("description", "")

        async with self._db.begin_nested():
            agent = await self._agents.create(
                workspace_id=workspace_id,
                name=app_name,
                description=app_description,
                owner_id=owner_id,
                tags=[],
                classification=Classification.INTERNAL,
                language="en",
                dify_dsl=dsl,
            )
            await self._versions.create(
                agent_id=agent.id,
                version_num=1,
                name=app_name,
                description=app_description,
                dify_dsl=dsl,
                changed_by=owner_id,
                change_note="Initial import",
            )
            await self._imports.record(
                workspace_id=workspace_id,
                imported_by=owner_id,
                source=AgentSource.YAML_UPLOAD,
                status="success",
                agent_id=agent.id,
                filename=filename,
                raw_yaml=raw_yaml.decode(errors="replace"),
            )
            await self._audit.record(
                action=Action.AGENT_CREATE,
                outcome=Outcome.SUCCESS,
                actor_user_id=owner_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
            )

        return agent

    async def list_dify_apps(self) -> list[dict[str, Any]]:
        """List the connected Dify workspace's apps for the import picker."""
        try:
            return await self._runtime.list_apps()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="Couldn't reach Dify"
            ) from exc

    async def import_from_dify_app(
        self, *, workspace_id: uuid.UUID, owner_id: uuid.UUID, dify_app_id: str
    ) -> Agent:
        """Export a Dify app's DSL and import it as a new Draft + Unvetted agent.

        Reuses the YAML import path (validation, versioning, audit), so the result
        is identical to an upload and must still pass the gatekeeper before publish.
        """
        try:
            dsl_yaml = await self._runtime.export_app_dsl(dify_app_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Dify app not found"
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="Couldn't reach Dify"
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="Couldn't reach Dify"
            ) from exc
        return await self.import_from_yaml(
            workspace_id=workspace_id,
            owner_id=owner_id,
            raw_yaml=dsl_yaml.encode("utf-8"),
            filename=f"dify-{dify_app_id}.yaml",
        )

    async def register_workflow_agent(
        self,
        *,
        workspace_id: uuid.UUID,
        owner_id: uuid.UUID,
        workflow_id: str,
        definition: dict[str, Any],
        config: dict[str, Any] | None = None,
        brief: str | None = None,
    ) -> Agent:
        """Register an n8n workflow as a workflow agent (Draft + Unvetted).

        The caller (endpoint) fetches `definition` via the user's own studio
        session, so a user can only register workflows they can access. The
        definition is stored as the version snapshot (same column the chat
        agents use) so versioning, vetting, and diffing work unchanged.
        O(n) over nodes for validation.
        """
        if len(json.dumps(definition)) > WORKFLOW_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Workflow definition exceeds 1 MiB limit",
            )
        definition, errors = validate_workflow(definition)
        if errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"errors": errors},
            )

        raw_name = str(definition.get("name") or "Workflow agent").strip()
        name = raw_name[:100] if len(raw_name) >= 2 else "Workflow agent"
        description = (brief or "Automation workflow published from the studio.")[:500]

        async with self._db.begin_nested():
            # config (publisher_config) is the editable source of truth for the workspace
            # card + the safe-fields edit path — distinct from the n8n definition (rebuilt
            # FROM the config on edit).
            agent = await self._agents.create(
                workspace_id=workspace_id,
                name=name,
                description=description,
                owner_id=owner_id,
                tags=[],
                classification=Classification.INTERNAL,
                language="en",
                dify_dsl=definition,
                kind=AgentKind.WORKFLOW,
                n8n_workflow_id=workflow_id,
                config=config,
            )
            await self._versions.create(
                agent_id=agent.id,
                version_num=1,
                name=name,
                description=agent.description,
                dify_dsl=definition,
                changed_by=owner_id,
                change_note="Registered from workflow studio",
            )
            await self._audit.record(
                action=Action.AGENT_CREATE,
                outcome=Outcome.SUCCESS,
                actor_user_id=owner_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
                metadata={"kind": AgentKind.WORKFLOW, "workflow_id": workflow_id},
            )
        return agent

    async def import_from_template(
        self,
        *,
        workspace_id: uuid.UUID,
        owner_id: uuid.UUID,
        template_id: str,
        templates: dict[str, dict],  # type: ignore[type-arg]
    ) -> Agent:
        """Create agent from a built-in template. O(1)."""
        template = templates.get(template_id)
        if not template:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template '{template_id}' not found",
            )

        dsl = template["dsl"]
        app_name = dsl.get("app", {}).get("name", template_id)
        description = template.get("description", "")

        async with self._db.begin_nested():
            agent = await self._agents.create(
                workspace_id=workspace_id,
                name=app_name,
                description=description,
                owner_id=owner_id,
                tags=[],
                classification=Classification.INTERNAL,
                language="en",
                dify_dsl=dsl,
            )
            await self._versions.create(
                agent_id=agent.id,
                version_num=1,
                name=app_name,
                description=description,
                dify_dsl=dsl,
                changed_by=owner_id,
                change_note=f"Created from template '{template_id}'",
            )
            await self._imports.record(
                workspace_id=workspace_id,
                imported_by=owner_id,
                source=AgentSource.TEMPLATE,
                status="success",
                agent_id=agent.id,
                filename=template_id,
            )
            await self._audit.record(
                action=Action.AGENT_CREATE,
                outcome=Outcome.SUCCESS,
                actor_user_id=owner_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
            )

        return agent

    # ------------------------------------------------------------------
    # Update (draft only)
    # ------------------------------------------------------------------

    async def update(
        self,
        *,
        agent: Agent,
        actor_id: uuid.UUID,
        name: str | None = None,
        description: str | None = None,
        dify_dsl: dict | None = None,  # type: ignore[type-arg]
        change_note: str = "",
    ) -> Agent:
        """Update a draft agent. Creates a new version snapshot. O(1)."""
        if agent.status != AgentStatus.DRAFT:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only draft agents can be edited",
            )

        new_version_num = await self._versions.next_version_num(agent.id)
        new_name = name if name is not None else agent.name
        new_description = description if description is not None else agent.description
        new_dsl = dify_dsl if dify_dsl is not None else {}

        async with self._db.begin_nested():
            await self._versions.create(
                agent_id=agent.id,
                version_num=new_version_num,
                name=new_name,
                description=new_description,
                dify_dsl=new_dsl,
                changed_by=actor_id,
                change_note=change_note,
            )
            fields: dict[str, object] = {"version": new_version_num}
            if name is not None:
                fields["name"] = name
            if description is not None:
                fields["description"] = description
            # Phase 6: any edit invalidates the prior vetting decision.
            # Approved → unvetted forces a fresh submit-for-review before publish.
            if agent.vetting_status in ("approved", "ready_for_review", "rejected"):
                fields["vetting_status"] = "unvetted"
            # A DSL change invalidates the registered Dify app — the new version
            # snapshot holds the DSL, so just clear dify_app_id to force a
            # re-register on the next sandbox test (Phase 14).
            if dify_dsl is not None:
                fields["dify_app_id"] = None
            agent = await self._agents.update(agent, **fields)
            await self._audit.record(
                action=Action.AGENT_UPDATE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=agent.workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
                metadata={"version": new_version_num},
            )

        # Re-fetch fresh after savepoint exit — same MissingGreenlet pattern
        # as publish/archive/rollback/transfer.
        fresh = await self._agents.get(agent.id, agent.workspace_id)
        if fresh is not None:
            agent = fresh
        return agent

    async def apply_config_update(
        self,
        *,
        agent: Agent,
        actor_id: uuid.UUID,
        new_config: dict[str, Any],
        definition: dict[str, Any],
        name: str | None = None,
        brief: str | None = None,
        revet: bool = False,
    ) -> Agent:
        """Safe-fields edit of a built workflow agent (Prompt 2, Slice 1b).

        Writes the new publisher_config + a new version snapshot of the rebuilt n8n
        definition. UNLIKE update(), this allows editing a non-draft (live/approved)
        agent (D2) — a schedule/destination edit keeps the vetting decision. A doc-set
        change (`revet=True`) is the ONLY thing that bounces it back to `unvetted`
        (the Draft+Unvetted invariant — a knowledge-surface change re-enters review).
        """
        new_version_num = await self._versions.next_version_num(agent.id)
        new_name = name if name is not None else agent.name
        async with self._db.begin_nested():
            await self._versions.create(
                agent_id=agent.id,
                version_num=new_version_num,
                name=new_name,
                description=brief if brief is not None else agent.description,
                dify_dsl=definition,
                changed_by=actor_id,
                change_note="config edit (re-vet)" if revet else "config edit",
            )
            fields: dict[str, object] = {"version": new_version_num, "config": new_config}
            if name is not None:
                fields["name"] = name
            if brief is not None:
                fields["description"] = brief
            # Only a doc-set change re-vets; schedule/destination stays as-is (in place).
            if revet and agent.vetting_status in ("approved", "ready_for_review", "rejected"):
                fields["vetting_status"] = "unvetted"
            agent = await self._agents.update(agent, **fields)
            await self._audit.record(
                action=Action.AGENT_UPDATE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=agent.workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
                metadata={"version": new_version_num, "revet": revet, "edit": "config"},
            )
        fresh = await self._agents.get(agent.id, agent.workspace_id)
        return fresh if fresh is not None else agent

    async def set_activation(self, *, agent: Agent, actor_id: uuid.UUID, active: bool) -> Agent:
        """Persist an agent's live/paused state in config.active + audit it. The n8n
        workflow activation itself is done by the caller (it holds the n8n session);
        this only records the intent so the UI + list reflect it without an n8n call.
        A teammate flipping a shared agent is recorded as the actor, with the
        owner it acted on behalf of (run-on-owner's-creds team sharing)."""
        new_cfg = {**(agent.config or {}), "active": active}
        meta: dict[str, object] = {"active": active}
        if agent.owner_id != actor_id:
            meta["on_behalf_of"] = str(agent.owner_id)
        async with self._db.begin_nested():
            agent = await self._agents.update(agent, config=new_cfg)
            await self._audit.record(
                action=Action.AGENT_UPDATE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=agent.workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
                metadata=meta,
            )
        fresh = await self._agents.get(agent.id, agent.workspace_id)
        return fresh if fresh is not None else agent

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def publish(
        self,
        *,
        agent: Agent,
        actor_id: uuid.UUID,
        bazaar_svc: BazaarService | None = None,
        background_tasks: BackgroundTasks | None = None,
        include_docs: bool = False,
    ) -> Agent:
        """DRAFT / IN_REVIEW → PUBLISHED. O(1).

        Phase 6 gate: agent.vetting_status must be 'approved' before publish.
        Bypassing the gatekeeper is impossible from this code path.

        `include_docs` (workflow agents): the publisher's explicit opt-in to ship
        the agent's documents with it — hirers run on the SAME data (copied into
        their own workspace at hire). Classification-gated: only Public/Internal
        may share; silently ignored when there are no docs to share.

        If bazaar_svc + background_tasks are provided, indexes the agent
        into Meilisearch as a fire-and-forget background task.
        """
        self._assert_transition(agent, AgentStatus.PUBLISHED)

        if include_docs and not (agent.kind == "workflow" and (agent.config or {}).get("docs")):
            include_docs = False  # nothing to share
        if include_docs and agent.classification not in ("public", "internal"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Only Public or Internal agents may share their documents with "
                    f"hirers — this agent is classified {agent.classification!r}."
                ),
            )

        # The vetting background task writes vetting_status from a *different*
        # session, so the snapshot loaded by _get_agent may be stale. Query
        # directly rather than `await db.refresh(agent)` — refresh detaches
        # the object on some asyncpg paths and breaks downstream from_orm.
        from sqlalchemy import select as _select

        current_vetting = (
            await self._db.execute(_select(Agent.vetting_status).where(Agent.id == agent.id))
        ).scalar_one()

        if current_vetting != "approved":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Agent vetting_status is {current_vetting!r}; only 'approved' agents "
                    "may be published. Submit for review first."
                ),
            )
        # Sync the cached value so from_orm sees the fresh state.
        agent.vetting_status = current_vetting

        async with self._db.begin_nested():
            fields: dict[str, object] = {"status": AgentStatus.PUBLISHED}
            if include_docs:
                fields["config"] = {**(agent.config or {}), "share_docs": True}
            agent = await self._agents.update(agent, **fields)
            await self._audit.record(
                action=Action.AGENT_PUBLISH,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=agent.workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
                metadata={"include_docs": include_docs},
            )

        # Re-fetch fresh from DB so serializers don't trip over lazy-loaded
        # attributes (the savepoint release can leave the object in a state
        # where SQLAlchemy wants to re-fetch by PK on attribute access).
        fresh = await self._agents.get(agent.id, agent.workspace_id)
        if fresh is not None:
            agent = fresh

        if bazaar_svc and background_tasks:
            background_tasks.add_task(bazaar_svc.index_agent, agent)

        return agent

    async def archive(
        self,
        *,
        agent: Agent,
        actor_id: uuid.UUID,
        bazaar_svc: BazaarService | None = None,
        background_tasks: BackgroundTasks | None = None,
    ) -> Agent:
        """DRAFT / PUBLISHED → ARCHIVED. O(1).

        If bazaar_svc + background_tasks are provided, removes the agent
        from the Meilisearch index as a fire-and-forget background task.
        """
        self._assert_transition(agent, AgentStatus.ARCHIVED)

        async with self._db.begin_nested():
            agent = await self._agents.update(agent, status=AgentStatus.ARCHIVED)
            await self._audit.record(
                action=Action.AGENT_ARCHIVE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=agent.workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
            )

        # See note in publish(): re-fetch after savepoint so AgentOut.from_orm
        # doesn't trip a lazy load (MissingGreenlet) on response serialization.
        fresh = await self._agents.get(agent.id, agent.workspace_id)
        if fresh is not None:
            agent = fresh

        if bazaar_svc and background_tasks:
            background_tasks.add_task(bazaar_svc.deindex_agent, agent.id)

        return agent

    async def rollback(self, *, agent: Agent, actor_id: uuid.UUID, version_num: int) -> Agent:
        """Create a new DRAFT from a previous version snapshot. Non-destructive. O(1)."""
        snap = await self._versions.get(agent.id, version_num)
        if not snap:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Version {version_num} not found",
            )

        new_version_num = await self._versions.next_version_num(agent.id)

        async with self._db.begin_nested():
            await self._versions.create(
                agent_id=agent.id,
                version_num=new_version_num,
                name=snap.name,
                description=snap.description,
                dify_dsl=snap.dify_dsl,
                changed_by=actor_id,
                change_note=f"Rollback from version {version_num}",
            )
            agent = await self._agents.update(
                agent,
                status=AgentStatus.DRAFT,
                version=new_version_num,
                name=snap.name,
                description=snap.description,
                # The new version snapshot (created above) restores the DSL;
                # clear dify_app_id to force a re-register on the next test.
                dify_app_id=None,
            )
            await self._audit.record(
                action=Action.AGENT_UPDATE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=agent.workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
                metadata={"rollback_from": version_num, "new_version": new_version_num},
            )

        # Same re-fetch pattern as publish/archive — avoid MissingGreenlet on response.
        fresh = await self._agents.get(agent.id, agent.workspace_id)
        if fresh is not None:
            agent = fresh
        return agent

    async def clone(self, *, agent: Agent, actor_id: uuid.UUID, workspace_id: uuid.UUID) -> Agent:
        """Fork an agent into a new DRAFT in the same workspace. O(1)."""
        latest_version = await self._versions.get(agent.id, agent.version)
        dsl = latest_version.dify_dsl if latest_version else {}

        async with self._db.begin_nested():
            clone = await self._agents.create(
                workspace_id=workspace_id,
                name=f"{agent.name} (copy)",
                description=agent.description,
                owner_id=actor_id,
                tags=list(agent.tags),
                classification=agent.classification,
                language=agent.language,
                dify_dsl=dsl,
            )
            await self._versions.create(
                agent_id=clone.id,
                version_num=1,
                name=clone.name,
                description=clone.description,
                dify_dsl=dsl,
                changed_by=actor_id,
                change_note=f"Cloned from agent {agent.id}",
            )
            await self._audit.record(
                action=Action.AGENT_CLONE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=clone.id,
                metadata={"source_agent_id": str(agent.id)},
            )

        return clone

    async def transfer(
        self, *, agent: Agent, actor_id: uuid.UUID, new_owner_id: uuid.UUID
    ) -> Agent:
        """Change agent owner. Admin only (enforced by OPA at endpoint layer). O(1)."""
        async with self._db.begin_nested():
            prev_owner = agent.owner_id
            agent = await self._agents.update(agent, owner_id=new_owner_id)
            await self._audit.record(
                action=Action.AGENT_TRANSFER,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=agent.workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
                metadata={"from_owner": str(prev_owner), "to_owner": str(new_owner_id)},
            )

        # Same re-fetch pattern as publish/archive — avoid MissingGreenlet on response.
        fresh = await self._agents.get(agent.id, agent.workspace_id)
        if fresh is not None:
            agent = fresh

        return agent

    # ------------------------------------------------------------------
    # Sandbox test
    # ------------------------------------------------------------------

    async def test_sandbox(
        self,
        *,
        agent: Agent,
        actor_id: uuid.UUID,
        query: str,
    ) -> dict:  # type: ignore[type-arg]
        """Invoke agent in sandbox. Enforces daily quota. O(log n) quota check."""
        uses_today = await self._agents.count_sandbox_uses_today(actor_id, agent.workspace_id)
        if uses_today >= settings.agent_sandbox_daily_quota:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Sandbox quota reached: {settings.agent_sandbox_daily_quota}"
                    " invocations per day"
                ),
            )

        if agent.kind == AgentKind.WORKFLOW:
            result = await self._invoke_workflow(agent, {"query": query})
        else:
            app_id = await self._ensure_registered(agent)
            result = await self._runtime.invoke_sandbox(
                app_id=app_id,
                query=query,
                user_id=str(actor_id),
            )

        await self._audit.record(
            action=Action.AGENT_TEST,
            outcome=Outcome.SUCCESS,
            actor_user_id=actor_id,
            actor_workspace_id=agent.workspace_id,
            resource_type=ResourceType.AGENT,
            resource_id=agent.id,
            metadata=_usage_metadata(result.get("usage", {}), agent_id=agent.id),
        )

        return result

    async def stream_sandbox(
        self,
        *,
        agent: Agent,
        actor_id: uuid.UUID,
        query: str,
    ) -> AsyncIterator[dict]:  # type: ignore[type-arg]
        """Streaming sandbox invocation.

        Same daily quota + lazy Dify registration as ``test_sandbox``, but
        async-yields ``{"token": str}`` chunks then a final ``{"usage": {...}}``.
        The ``agent.test`` audit row (which *is* the quota ledger) is written on
        a **fresh** session only on successful completion — the request
        transaction is already committed once streaming begins, and an
        incomplete/cancelled stream must not burn quota. O(log n) quota check.
        """
        uses_today = await self._agents.count_sandbox_uses_today(actor_id, agent.workspace_id)
        if uses_today >= settings.agent_sandbox_daily_quota:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Sandbox quota reached: {settings.agent_sandbox_daily_quota}"
                    " invocations per day"
                ),
            )

        # Workflow agents don't stream — run once and emit the result as a
        # single token + usage so the same SSE pipeline serves both kinds.
        if agent.kind == AgentKind.WORKFLOW:
            result = await self._invoke_workflow(agent, {"query": query})
            source: AsyncIterator[dict] = _as_stream(result)  # type: ignore[type-arg]
        else:
            app_id = await self._ensure_registered(agent)
            source = self._runtime.stream_sandbox(app_id=app_id, query=query, user_id=str(actor_id))

        # Capture before the first yield: after it, the request session is closed.
        workspace_id = agent.workspace_id
        agent_id = agent.id
        completed = False
        usage_meta: dict = {}  # type: ignore[type-arg]
        try:
            async for item in source:
                if "usage" in item:
                    usage_meta = item["usage"]
                yield item
            completed = True
        finally:
            if completed:
                async with AsyncSessionLocal.begin() as audit_db:
                    await AuditRepository(audit_db).record(
                        action=Action.AGENT_TEST,
                        outcome=Outcome.SUCCESS,
                        actor_user_id=actor_id,
                        actor_workspace_id=workspace_id,
                        resource_type=ResourceType.AGENT,
                        resource_id=agent_id,
                        metadata=_usage_metadata(usage_meta, agent_id=agent_id),
                    )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _invoke_workflow(self, agent: Agent, payload: dict[str, Any]) -> dict[str, Any]:
        """Run a workflow agent via its webhook; map failures to clean HTTP errors.

        404 from the webhook almost always means the workflow isn't active in
        the studio — say so instead of a generic 502. O(1) network.
        """
        version = await self._versions.get(agent.id, agent.version)
        definition = version.dify_dsl if version else None
        if not definition:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Agent has no workflow definition",
            )
        try:
            return await self._workflow_runtime.invoke_workflow(definition, payload)
        except WorkflowNotInvokableError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
            ) from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "The workflow isn't active yet — activate it in the studio "
                        "(toggle it on), then run again"
                    ),
                ) from e
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="Workflow run failed"
            ) from e
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Workflow engine unreachable",
            ) from e

    async def _ensure_registered(self, agent: Agent) -> str:
        """Return a usable Dify app_id, registering the agent lazily on first use.

        Fail-closed: 503 if the runtime is unconfigured (no console token) or
        unreachable, 409 if the agent has no DSL. Never returns None, never leaks
        the Dify response body. O(1) — one extra POST only on the first test.
        """
        if agent.dify_app_id:
            return agent.dify_app_id
        if not settings.dify_console_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Agent runtime is not configured",
            )
        # The DSL lives on the current version snapshot (AgentVersion), not the
        # Agent row.
        version = await self._versions.get(agent.id, agent.version)
        dsl = version.dify_dsl if version else None
        if not dsl:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Agent has no definition to register",
            )
        try:
            app_id = await self._runtime.register_app(agent.name, dsl)
        except AgentDefinitionError as e:
            # The DSL is invalid (e.g. an old simplified template, not a real
            # Dify export) — that's a 4xx, not a runtime outage.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"This agent's definition can't run on the agent runtime: {e}",
            ) from e
        except httpx.HTTPError as e:
            logger.warning("Dify register_app failed for agent %s: %s", agent.id, e)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Agent runtime unavailable",
            ) from e
        async with self._db.begin_nested():
            await self._agents.update(agent, dify_app_id=app_id)
        return app_id

    def _assert_transition(self, agent: Agent, target: AgentStatus) -> None:
        current = AgentStatus(agent.status)
        allowed = _ALLOWED_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot transition from {current} to {target}",
            )
