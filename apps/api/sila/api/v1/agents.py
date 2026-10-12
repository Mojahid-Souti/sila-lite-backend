"""Agent lifecycle endpoints.

All endpoints are workspace-scoped.
Auth enforced via require_workspace_role (same pattern as workspaces.py).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

import httpx
import yaml
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.agent_runtime.dify import DifyAdapter
from sila.adapters.auth.base import UserResult
from sila.adapters.n8n.base import N8nService
from sila.api.deps import get_current_user, get_n8n_service, require_workspace_role
from sila.core.config import settings
from sila.core.constants import Role
from sila.db.models import Agent, AgentVersion
from sila.db.repositories.agent import AgentRepository
from sila.db.repositories.agent_version import AgentVersionRepository
from sila.db.session import get_db
from sila.services import n8n_provisioning
from sila.services.agent_service import AgentService
from sila.services.sila.orchestrator.runner import tier_for
from sila.services.vetting_service import VettingService

router = APIRouter()
logger = logging.getLogger(__name__)

_YAML_MAX_BYTES = 1_048_576  # 1 MiB — also enforced in yaml_validator but checked here first


# ---------------------------------------------------------------------------
# Template loader (runs once at startup, cached in module scope)
# ---------------------------------------------------------------------------


def _load_templates() -> dict[str, dict[str, Any]]:
    """Load all YAML files from the templates/ directory. O(t) where t = template count."""
    templates: dict[str, dict[str, Any]] = {}
    templates_dir = Path(__file__).parent.parent.parent / "templates"
    if not templates_dir.is_dir():
        return templates
    for path in templates_dir.glob("*.yaml"):
        try:
            with path.open("rb") as f:
                dsl = yaml.safe_load(f)
            template_id = path.stem
            app = dsl.get("app", {}) or {}
            # `sila_metadata` is a Sila-side block — Dify ignores unknown
            # top-level keys, so it travels with the YAML without polluting it.
            meta = dsl.get("sila_metadata", {}) or {}
            templates[template_id] = {
                "id": template_id,
                "name": app.get("name", template_id),
                "description": app.get("description", ""),
                "icon_emoji": app.get("icon", ""),
                "icon_background": app.get("icon_background", "#F5F5F5"),
                "icon_name": meta.get("icon_name", "Sparkles"),
                "category": meta.get("category", "Other"),
                "classification": meta.get("classification", "Internal"),
                "connectors": list(meta.get("connectors", [])),
                "tags": list(meta.get("tags", [])),
                "use_count": int(meta.get("use_count", 0)),
                "featured": bool(meta.get("featured", False)),
                "sample_prompts": list(dsl.get("suggested_questions", []) or []),
                "dsl": dsl,
            }
        except Exception:
            pass  # Skip malformed template files silently at startup
    return templates


_TEMPLATES: dict[str, dict[str, Any]] = _load_templates()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


def _status_label(status: str, vetting_status: str) -> str:
    """A friendly status for the card/badge (everyone sees this; Expert also sees the
    raw vetting_status). Maps the (status, vetting_status) pair to one human phrase."""
    if status == "published":
        return "Live"
    if status == "archived":
        return "Archived"
    if vetting_status == "approved":
        return "Ready"
    if vetting_status == "ready_for_review":
        return "Pending review"
    if vetting_status == "rejected":
        return "Needs changes"
    if vetting_status == "scanning":
        return "Reviewing…"
    return "Draft — pending review"


class AgentOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    description: str  # the short brief shown on the card
    owner_id: uuid.UUID
    tags: list[str]
    status: str
    status_label: str  # friendly status (all tiers)
    language: str
    classification: str
    kind: str
    publisher_config: dict[str, Any] | None  # schedule/destinations/docs (editable)
    created_at: str
    updated_at: str
    # Expert-only (dropped server-side for a Beginner socket — tier ≠ privilege):
    version: int | None = None
    vetting_status: str | None = None
    dify_app_id: str | None = None

    @classmethod
    def from_orm(cls, a: Agent, tier: str = "expert") -> AgentOut:
        config = a.config
        expert = tier == "expert"
        return cls(
            id=a.id,
            workspace_id=a.workspace_id,
            name=a.name,
            description=a.description,
            owner_id=a.owner_id,
            tags=list(a.tags),
            status=a.status,
            status_label=_status_label(a.status, a.vetting_status),
            language=a.language,
            classification=a.classification,
            kind=a.kind,
            publisher_config=config if isinstance(config, dict) else None,
            created_at=a.created_at.isoformat(),
            updated_at=a.updated_at.isoformat(),
            # Expert-only detail — omitted for a Beginner (enforced here, not in CSS).
            version=a.version if expert else None,
            vetting_status=a.vetting_status if expert else None,
            dify_app_id=a.dify_app_id if expert else None,
        )


class AgentVersionOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    version_num: int
    name: str
    description: str
    changed_by: uuid.UUID
    change_note: str
    created_at: str

    @classmethod
    def from_orm(cls, v: AgentVersion) -> AgentVersionOut:
        return cls(
            id=v.id,
            agent_id=v.agent_id,
            version_num=v.version_num,
            name=v.name,
            description=v.description,
            changed_by=v.changed_by,
            change_note=v.change_note,
            created_at=v.created_at.isoformat(),
        )


class AgentListOut(BaseModel):
    items: list[AgentOut]
    total: int
    page: int
    size: int


class UpdateAgentIn(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=100)
    description: str | None = None
    change_note: str = ""


class TransferAgentIn(BaseModel):
    new_owner_id: uuid.UUID


class ImportFromTemplateIn(BaseModel):
    template_id: str


class ImportFromDifyIn(BaseModel):
    dify_app_id: str = Field(..., min_length=1, max_length=100)


class RegisterWorkflowIn(BaseModel):
    workflow_id: str = Field(..., min_length=1, max_length=100)


class PublishIn(BaseModel):
    """Optional publish options. `include_docs` = the publisher's explicit opt-in
    to ship the agent's documents with it (hirers run on the same data, copied
    into their workspace) — classification-gated server-side."""

    include_docs: bool = False


class WorkflowSourceOut(BaseModel):
    """One workflow available to publish as an agent (engine-neutral shape)."""

    id: str
    name: str
    active: bool
    updated_at: str | None


class RegisterWorkflowOut(BaseModel):
    agent: AgentOut
    vetting_run_id: uuid.UUID


class DifyAppOut(BaseModel):
    id: str
    name: str
    mode: str
    description: str


class TestAgentIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)


class TemplateOut(BaseModel):
    id: str
    name: str
    description: str
    icon_emoji: str = ""
    icon_background: str = "#F5F5F5"
    icon_name: str = "Sparkles"
    category: str = "Other"
    classification: str = "Internal"
    connectors: list[str] = []
    tags: list[str] = []
    use_count: int = 0
    featured: bool = False
    sample_prompts: list[str] = []


# ---------------------------------------------------------------------------
# Dependency: resolve agent within the caller's workspace
# ---------------------------------------------------------------------------


async def _get_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Agent:
    repo = AgentRepository(db)
    agent = await repo.get(agent_id, workspace_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


def _runtime() -> DifyAdapter:
    return DifyAdapter()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/workspaces/{workspace_id}/agents", response_model=AgentListOut)
async def list_agents(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: str | None = None,
    page: int = 1,
    size: int = 20,
) -> AgentListOut:
    """List agents in workspace, paginated. O(log n) via composite index."""
    if size > 100:
        size = 100
    _user, role = caller
    tier = tier_for(role)
    repo = AgentRepository(db)
    items, total = await repo.list(workspace_id, status=status_filter, page=page, size=size)
    return AgentListOut(
        items=[AgentOut.from_orm(a, tier) for a in items],
        total=total,
        page=page,
        size=size,
    )


@router.post(
    "/workspaces/{workspace_id}/agent-imports",
    response_model=AgentOut,
    status_code=status.HTTP_201_CREATED,
)
async def import_agent_yaml(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),  # noqa: B008
) -> AgentOut:
    """Import agent from Dify DSL YAML upload. O(s) where s = YAML size."""
    user, _ = caller
    raw = await file.read(_YAML_MAX_BYTES + 1)
    if len(raw) > _YAML_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="YAML file exceeds 1 MiB limit",
        )
    svc = AgentService(db, _runtime())
    agent = await svc.import_from_yaml(
        workspace_id=workspace_id,
        owner_id=user.id,
        raw_yaml=raw,
        filename=file.filename or "upload.yaml",
    )
    return AgentOut.from_orm(agent)


@router.post(
    "/workspaces/{workspace_id}/agent-imports/template",
    response_model=AgentOut,
    status_code=status.HTTP_201_CREATED,
)
async def import_agent_template(
    workspace_id: uuid.UUID,
    body: ImportFromTemplateIn,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentOut:
    """Create agent from a built-in template. O(1)."""
    user, _ = caller
    svc = AgentService(db, _runtime())
    agent = await svc.import_from_template(
        workspace_id=workspace_id,
        owner_id=user.id,
        template_id=body.template_id,
        templates=_TEMPLATES,
    )
    return AgentOut.from_orm(agent)


@router.get("/workspaces/{workspace_id}/dify-apps", response_model=list[DifyAppOut])
async def list_dify_apps(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[DifyAppOut]:
    """List the connected Dify workspace's apps for the Build-in-Dify import picker.

    NOTE: Dify runs as a single shared workspace in the POC, so this lists every
    app in that workspace (not just the caller's). The import still lands the
    agent in this Sila workspace. Per-workspace Dify projects are a prod concern.
    """
    svc = AgentService(db, _runtime())
    apps = await svc.list_dify_apps()
    return [DifyAppOut(**a) for a in apps]


@router.post(
    "/workspaces/{workspace_id}/agents/import-from-dify",
    response_model=AgentOut,
    status_code=status.HTTP_201_CREATED,
)
async def import_agent_from_dify(
    workspace_id: uuid.UUID,
    body: ImportFromDifyIn,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentOut:
    """Export a Dify app's DSL and import it as a Draft + Unvetted agent. O(s)."""
    user, _ = caller
    svc = AgentService(db, _runtime())
    agent = await svc.import_from_dify_app(
        workspace_id=workspace_id,
        owner_id=user.id,
        dify_app_id=body.dify_app_id,
    )
    return AgentOut.from_orm(agent)


@router.get(
    "/workspaces/{workspace_id}/agents/workflow-sources",
    response_model=list[WorkflowSourceOut],
)
async def list_workflow_sources(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    n8n: Annotated[N8nService, Depends(get_n8n_service)],
) -> list[WorkflowSourceOut]:
    """The caller's own studio workflows, for the publish-as-agent picker.

    Uses the caller's provisioned studio session — a user only ever sees
    their own workflows. O(1) network.
    """
    user, _ = caller
    session = await n8n_provisioning.ensure_session(
        db=db, n8n=n8n, supabase_user_id=user.id, sila_full_name=None
    )
    try:
        workflows = await n8n.list_workflows(session.cookie_value)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Workflow engine unreachable"
        ) from exc
    return [
        WorkflowSourceOut(id=w.id, name=w.name, active=w.active, updated_at=w.updated_at)
        for w in workflows
    ]


@router.post(
    "/workspaces/{workspace_id}/agents/register-workflow",
    response_model=RegisterWorkflowOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_workflow_agent(
    workspace_id: uuid.UUID,
    body: RegisterWorkflowIn,
    background_tasks: BackgroundTasks,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    n8n: Annotated[N8nService, Depends(get_n8n_service)],
) -> RegisterWorkflowOut:
    """Publish a studio workflow as a workflow agent.

    Fetches the workflow via the caller's OWN studio session (no cross-user
    access), registers it as Draft + Unvetted, and immediately submits it to
    the security-agent pipeline (Phase 6) — the publish modal polls the
    returned vetting run for progress. O(n) over workflow nodes.
    """
    user, _ = caller
    session = await n8n_provisioning.ensure_session(
        db=db, n8n=n8n, supabase_user_id=user.id, sila_full_name=None
    )
    try:
        definition = await n8n.get_workflow(session.cookie_value, body.workflow_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Workflow engine unreachable"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Workflow engine unreachable"
        ) from exc

    svc = AgentService(db, _runtime())
    agent = await svc.register_workflow_agent(
        workspace_id=workspace_id,
        owner_id=user.id,
        workflow_id=body.workflow_id,
        definition=definition,
    )
    # Snapshot the agent before submit_for_review commits — the commit expires
    # the ORM object, and a later attribute access would lazy-load on a closed
    # transaction. The modal only needs agent.id + the run id.
    agent_out = AgentOut.from_orm(agent)
    run = await VettingService(db).submit_for_review(
        agent=agent, actor_id=user.id, background_tasks=background_tasks
    )
    return RegisterWorkflowOut(agent=agent_out, vetting_run_id=run.id)


@router.post("/workspaces/{workspace_id}/agents/{agent_id}/test-run")
async def test_run_workflow_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    n8n: Annotated[N8nService, Depends(get_n8n_service)],
) -> dict[str, str]:
    """Fire a workflow agent ONCE on demand — the 'run now' for a scheduled agent that
    would otherwise only fire on its cron. Runs in the caller's OWN n8n session (no
    cross-user access); the agent must be a workflow agent. Returns the execution id."""
    user, _ = caller
    agent = await AgentRepository(db).get(agent_id, workspace_id)
    if agent is None or not agent.n8n_workflow_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow agent not found"
        )
    session = await n8n_provisioning.ensure_session(
        db=db, n8n=n8n, supabase_user_id=user.id, sila_full_name=None
    )
    try:
        execution_id = await n8n.run_workflow(session.cookie_value, agent.n8n_workflow_id)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Workflow engine unreachable"
        ) from exc
    return {"execution_id": execution_id}


@router.get("/workspaces/{workspace_id}/agents/{agent_id}", response_model=AgentOut)
async def get_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    agent: Annotated[Agent, Depends(_get_agent)],
) -> AgentOut:
    """Get agent detail. O(1) primary key lookup."""
    return AgentOut.from_orm(agent, tier_for(caller[1]))


class ScheduleIn(BaseModel):
    cron: str = Field(..., min_length=1, max_length=100)
    timezone: str = Field(default="Asia/Muscat", max_length=64)


class DestinationIn(BaseModel):
    type: str = Field(..., max_length=40)
    channel_id: str | None = Field(None, max_length=64)
    channel_name: str | None = Field(None, max_length=120)
    to: str | None = Field(None, max_length=320)  # email + gmail recipient
    spreadsheet_id: str | None = Field(None, max_length=128)  # google-sheets target
    space: str | None = Field(None, max_length=128)  # google-chat space resource name

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        """Accept slack/email + ANY manifest connector — the build path emits a
        destination for every wired connector, so a hardcoded whitelist here 422'd
        saves on agents with newer connectors (a google-docs agent couldn't be
        edited). The manifest is the single source of truth."""
        from sila.services.connectors.manifest import spec_for

        if value in ("slack", "email") or spec_for(value) is not None:
            return value
        raise ValueError(f"unknown destination type '{value}'")


class ConfigUpdateIn(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=100)
    schedule: ScheduleIn | None = None
    destinations: list[DestinationIn] | None = None
    docs: list[str] | None = None  # the new doc set (a change re-vets — D1)


class ConfigUpdateOut(BaseModel):
    agent: AgentOut
    revet: bool  # True → the doc-set change sent it back to review


def _brief_from_config(config: dict[str, Any]) -> str:
    from sila.services.sila.workflow_assembly import destination_label

    content = str(config.get("content") or "Summarizes your documents")
    dests = config.get("destinations") or []
    label = " and ".join(destination_label(d) for d in dests)
    return f"{content} — posts to {label}."[:500] if dests else content[:500]


@router.patch("/workspaces/{workspace_id}/agents/{agent_id}/config", response_model=ConfigUpdateOut)
async def update_agent_config(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    body: ConfigUpdateIn,
    background_tasks: BackgroundTasks,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    n8n: Annotated[N8nService, Depends(get_n8n_service)],
) -> ConfigUpdateOut:
    """Edit a built agent's BUSINESS fields only (name / schedule / destination / which
    docs) — never model/temp/graph. Schedule + destination apply in place (the live n8n
    workflow is rebuilt deactivate→update→reactivate); a DOC-SET change re-vets (D1:
    a knowledge-surface change re-enters review). The workflow is rebuilt through the
    build path (assembly), never hand-edited.

    TEAM SHARING (run-on-owner's-creds): the agent keeps its OWNER's identity no
    matter which Builder edits — capabilities re-mint against the OWNER's bindings
    and the n8n ops run under the OWNER's session. A gone owner connection fails
    closed with a 400 naming it (assembly alone would silently drop the node)."""
    from sila.services.sila.workflow_assembly import (
        build_definition_from_config,
        missing_oauth_bindings,
    )

    user, role = caller
    agent = await AgentRepository(db).get(agent_id, workspace_id)
    if agent is None or agent.kind != "workflow" or not agent.config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Editable agent not found"
        )
    owner_id: uuid.UUID = agent.owner_id

    cfg: dict[str, Any] = dict(agent.config)
    if body.schedule is not None:
        cfg["schedule"] = {"cron": body.schedule.cron, "timezone": body.schedule.timezone}
    if body.destinations is not None:
        cfg["destinations"] = [d.model_dump(exclude_none=True) for d in body.destinations]
    revet = body.docs is not None and set(body.docs) != set(cfg.get("docs") or [])
    if body.docs is not None:
        cfg["docs"] = body.docs

    missing = await missing_oauth_bindings(
        db=db,
        user_id=owner_id,
        workspace_id=workspace_id,
        destinations=list(cfg.get("destinations") or []),
    )
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"The owner's {', '.join(missing)} connection is missing — "
                "ask them to reconnect it on the Connectors page."
            ),
        )

    wf_name = body.name or agent.name
    definition = await build_definition_from_config(
        db=db, user_id=owner_id, workspace_id=workspace_id, name=wf_name, config=cfg
    )

    # Apply to the live workflow: deactivate → update → reactivate (n8n 2.x order).
    if agent.n8n_workflow_id:
        session = await n8n_provisioning.ensure_session(
            db=db, n8n=n8n, supabase_user_id=owner_id, sila_full_name=None
        )
        try:
            current = await n8n.get_workflow(session.cookie_value, agent.n8n_workflow_id)
            was_active = bool(current.get("active"))
            if was_active:
                await n8n.deactivate_workflow(session.cookie_value, agent.n8n_workflow_id)
            await n8n.update_workflow(session.cookie_value, agent.n8n_workflow_id, definition)
            if was_active:
                await n8n.activate_workflow(session.cookie_value, agent.n8n_workflow_id)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="Workflow engine unreachable"
            ) from exc

    svc = AgentService(db, _runtime())
    agent = await svc.apply_config_update(
        agent=agent,
        actor_id=user.id,
        new_config=cfg,
        definition=definition,
        name=body.name,
        brief=_brief_from_config(cfg),
        revet=revet,
    )
    agent_out = AgentOut.from_orm(agent, tier_for(role))
    if revet:
        await VettingService(db).submit_for_review(
            agent=agent, actor_id=user.id, background_tasks=background_tasks
        )
    return ConfigUpdateOut(agent=agent_out, revet=revet)


class ActivationIn(BaseModel):
    active: bool


@router.post("/workspaces/{workspace_id}/agents/{agent_id}/activation", response_model=AgentOut)
async def set_agent_activation(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    body: ActivationIn,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.HIRER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    n8n: Annotated[N8nService, Depends(get_n8n_service)],
) -> AgentOut:
    """Turn a built agent's schedule ON (activate its n8n workflow) or OFF (pause). Only
    an APPROVED agent may go live — Draft/Unvetted can never run (the hard invariant);
    pausing is always allowed.

    TEAM SHARING: any member Hirer+ may flip an approved shared agent (OPA
    `agent.activate` = hirer). The n8n op runs under the agent OWNER's session —
    the workflow lives in the owner's n8n account and keeps the owner's baked
    capabilities — while the audit records the actual caller (+ on_behalf_of)."""
    user, role = caller
    agent = await AgentRepository(db).get(agent_id, workspace_id)
    if agent is None or agent.kind != "workflow" or not agent.n8n_workflow_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Runnable agent not found"
        )
    if body.active and agent.vetting_status != "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only an approved agent can be turned on. It's still pending review.",
        )
    session = await n8n_provisioning.ensure_session(
        db=db, n8n=n8n, supabase_user_id=agent.owner_id, sila_full_name=None
    )
    try:
        if body.active:
            await n8n.activate_workflow(session.cookie_value, agent.n8n_workflow_id)
        else:
            await n8n.deactivate_workflow(session.cookie_value, agent.n8n_workflow_id)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Workflow engine unreachable"
        ) from exc
    agent = await AgentService(db, _runtime()).set_activation(
        agent=agent, actor_id=user.id, active=body.active
    )
    return AgentOut.from_orm(agent, tier_for(role))


@router.patch("/workspaces/{workspace_id}/agents/{agent_id}", response_model=AgentOut)
async def update_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    body: UpdateAgentIn,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(_get_agent)],
) -> AgentOut:
    """Update draft agent. Creates a new version snapshot. O(1)."""
    user, _ = caller
    svc = AgentService(db, _runtime())
    agent = await svc.update(
        agent=agent,
        actor_id=user.id,
        name=body.name,
        description=body.description,
        change_note=body.change_note,
    )
    return AgentOut.from_orm(agent)


@router.post(
    "/workspaces/{workspace_id}/agents/{agent_id}/publish",
    response_model=AgentOut,
)
async def publish_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(_get_agent)],
    background_tasks: BackgroundTasks,
    body: PublishIn | None = None,
) -> AgentOut:
    """Transition agent DRAFT → PUBLISHED. Indexes into Meilisearch as background task. O(1)."""
    from sila.adapters.search.meilisearch import MeilisearchAdapter
    from sila.services.bazaar_service import BazaarService

    user, _ = caller
    svc = AgentService(db, _runtime())
    search = MeilisearchAdapter(
        url=settings.meilisearch_url, master_key=settings.meilisearch_master_key
    )
    bazaar_svc = BazaarService(db, search)
    agent = await svc.publish(
        agent=agent,
        actor_id=user.id,
        bazaar_svc=bazaar_svc,
        background_tasks=background_tasks,
        include_docs=bool(body and body.include_docs),
    )
    return AgentOut.from_orm(agent)


@router.post(
    "/workspaces/{workspace_id}/agents/{agent_id}/archive",
    response_model=AgentOut,
)
async def archive_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(_get_agent)],
    background_tasks: BackgroundTasks,
) -> AgentOut:
    """Transition agent → ARCHIVED. Removes from Meilisearch as background task. O(1)."""
    from sila.adapters.search.meilisearch import MeilisearchAdapter
    from sila.services.bazaar_service import BazaarService

    user, _ = caller
    svc = AgentService(db, _runtime())
    search = MeilisearchAdapter(
        url=settings.meilisearch_url, master_key=settings.meilisearch_master_key
    )
    bazaar_svc = BazaarService(db, search)
    agent = await svc.archive(
        agent=agent,
        actor_id=user.id,
        bazaar_svc=bazaar_svc,
        background_tasks=background_tasks,
    )
    return AgentOut.from_orm(agent)


@router.post(
    "/workspaces/{workspace_id}/agents/{agent_id}/clone",
    response_model=AgentOut,
    status_code=status.HTTP_201_CREATED,
)
async def clone_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(_get_agent)],
) -> AgentOut:
    """Fork agent into new DRAFT in same workspace. O(1)."""
    user, _ = caller
    svc = AgentService(db, _runtime())
    clone = await svc.clone(agent=agent, actor_id=user.id, workspace_id=workspace_id)
    return AgentOut.from_orm(clone)


@router.post(
    "/workspaces/{workspace_id}/agents/{agent_id}/transfer",
    response_model=AgentOut,
)
async def transfer_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    body: TransferAgentIn,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(_get_agent)],
) -> AgentOut:
    """Transfer agent ownership. Admin only. O(1)."""
    user, _ = caller
    svc = AgentService(db, _runtime())
    agent = await svc.transfer(agent=agent, actor_id=user.id, new_owner_id=body.new_owner_id)
    return AgentOut.from_orm(agent)


class AgentYamlOut(BaseModel):
    yaml: str
    version: int


@router.get(
    "/workspaces/{workspace_id}/agents/{agent_id}/yaml",
    response_model=AgentYamlOut,
)
async def get_agent_yaml(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(_get_agent)],
) -> AgentYamlOut:
    """Return the agent's current-version DSL serialized as YAML.

    Used by the vetting page to show the source-of-truth alongside findings.
    O(1) — single DB lookup + in-memory yaml.safe_dump.
    """
    repo = AgentVersionRepository(db)
    versions = await repo.list(agent_id, page=1, size=1)
    if not versions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent has no versions yet",
        )
    head = versions[0]
    yaml_text = yaml.safe_dump(head.dify_dsl, default_flow_style=False, sort_keys=False)
    return AgentYamlOut(yaml=yaml_text, version=head.version_num)


@router.get(
    "/workspaces/{workspace_id}/agents/{agent_id}/versions",
    response_model=list[AgentVersionOut],
)
async def list_versions(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(_get_agent)],
    page: int = 1,
    size: int = 20,
) -> list[AgentVersionOut]:
    """List version history newest-first. O(k) where k = versions per agent (<50)."""
    if size > 100:
        size = 100
    repo = AgentVersionRepository(db)
    versions = await repo.list(agent_id, page=page, size=size)
    return [AgentVersionOut.from_orm(v) for v in versions]


@router.post(
    "/workspaces/{workspace_id}/agents/{agent_id}/versions/{version_num}/rollback",
    response_model=AgentOut,
)
async def rollback_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    version_num: int,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(_get_agent)],
) -> AgentOut:
    """Create new DRAFT from a version snapshot. Non-destructive. O(1)."""
    user, _ = caller
    svc = AgentService(db, _runtime())
    agent = await svc.rollback(agent=agent, actor_id=user.id, version_num=version_num)
    return AgentOut.from_orm(agent)


@router.post(
    "/workspaces/{workspace_id}/agents/{agent_id}/test",
    response_model=dict,
)
async def test_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    body: TestAgentIn,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(_get_agent)],
) -> dict:  # type: ignore[type-arg]
    """Sandbox non-streaming test. Quota: 100 invocations/day per user. O(log n) quota check."""
    user, _ = caller
    svc = AgentService(db, _runtime())
    return await svc.test_sandbox(agent=agent, actor_id=user.id, query=body.query)


@router.post("/workspaces/{workspace_id}/agents/{agent_id}/test-stream")
async def test_agent_stream(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    body: TestAgentIn,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[Agent, Depends(_get_agent)],
) -> StreamingResponse:
    """Sandbox streaming test (SSE). Same 100/day quota as ``/test``.

    Relays Dify's token stream as ``data: {"token": "…"}`` frames and a terminal
    ``data: {"done": true, "usage": {…}}``. Quota (429), registration (503/409),
    and other pre-stream failures surface as normal JSON status codes because the
    generator is primed once before the response starts. O(log n) quota check.
    """
    user, _ = caller
    svc = AgentService(db, _runtime())
    agen = svc.stream_sandbox(agent=agent, actor_id=user.id, query=body.query)

    # Prime once: quota/registration checks run here, so their HTTPExceptions are
    # raised before the 200 + body start and become proper 429/503/409 responses.
    try:
        first: dict[str, Any] | None = await agen.__anext__()
    except StopAsyncIteration:
        first = None

    def _frame(item: dict[str, Any]) -> str:
        if "usage" in item:
            return f"data: {json.dumps({'done': True, 'usage': item['usage']})}\n\n"
        return f"data: {json.dumps(item)}\n\n"

    async def _sse() -> AsyncIterator[str]:
        done_sent = False
        try:
            if first is not None:
                yield _frame(first)
                done_sent = "usage" in first
            async for item in agen:
                yield _frame(item)
                done_sent = done_sent or ("usage" in item)
            if not done_sent:  # only emit a terminal frame if message_end didn't
                yield 'data: {"done": true}\n\n'
        except Exception:
            logger.exception("agent test-stream failed mid-stream")
            yield 'data: {"error": "stream_failed"}\n\n'
            yield 'data: {"done": true}\n\n'

    return StreamingResponse(
        _sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(
    current_user: Annotated[UserResult, Depends(get_current_user)],
) -> list[TemplateOut]:
    """List built-in agent templates with gallery metadata. O(t)."""
    return [
        TemplateOut(
            id=t["id"],
            name=t["name"],
            description=t["description"],
            icon_emoji=t["icon_emoji"],
            icon_background=t["icon_background"],
            icon_name=t["icon_name"],
            category=t["category"],
            classification=t["classification"],
            connectors=t["connectors"],
            tags=t["tags"],
            use_count=t["use_count"],
            featured=t["featured"],
            sample_prompts=t["sample_prompts"],
        )
        for t in _TEMPLATES.values()
    ]
