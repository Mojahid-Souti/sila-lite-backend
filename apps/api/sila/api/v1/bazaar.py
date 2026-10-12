"""Bazaar endpoints — catalog, hire, and review.

Catalog endpoints (/v1/bazaar/*) are workspace-independent:
  any authenticated user in any workspace can browse published agents.
  The calling workspace_id is passed as a query param so the 'hired' flag
  is accurate for that workspace.

Hire endpoints (/v1/workspaces/{wid}/hires) are workspace-scoped.
Review endpoints (/v1/bazaar/agents/{aid}/reviews) use any valid workspace token.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.agent_runtime.dify import DifyAdapter
from sila.adapters.auth.base import UserResult
from sila.adapters.n8n.base import N8nService
from sila.adapters.search.meilisearch import MeilisearchAdapter
from sila.api.deps import get_current_user, get_n8n_service, require_workspace_role
from sila.api.v1.agents import AgentOut, DestinationIn, ScheduleIn
from sila.core.config import settings
from sila.core.constants import ReactionType, Role
from sila.db.repositories.bazaar import BazaarRepository
from sila.db.session import get_db
from sila.services import hire_provisioning
from sila.services.agent_service import AgentService
from sila.services.bazaar_service import BazaarService
from sila.services.vetting_service import VettingService

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependency — builds BazaarService with the Meilisearch adapter
# ---------------------------------------------------------------------------


def get_bazaar_service(db: Annotated[AsyncSession, Depends(get_db)]) -> BazaarService:
    search = MeilisearchAdapter(
        url=settings.meilisearch_url,
        master_key=settings.meilisearch_master_key,
    )
    return BazaarService(db, search)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ReviewIn(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    body: str = Field("", max_length=2000)


class ReactionIn(BaseModel):
    # null clears the caller's reaction; same value again toggles it off.
    reaction: ReactionType | None = None


class ReportIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class ProvisionHireIn(BaseModel):
    """The hirer's own setup for a workflow-agent hire: THEIR destinations and
    optionally their schedule/KB. The publisher's targets never appear here."""

    destinations: list[DestinationIn] = Field(..., min_length=1, max_length=5)
    schedule: ScheduleIn | None = None
    kb_id: uuid.UUID | None = None  # hirer's existing KB; None → ungrounded (warned)
    name: str | None = Field(None, min_length=2, max_length=100)


class ProvisionHireOut(BaseModel):
    agent: AgentOut
    vetting_run_id: uuid.UUID
    hire_id: uuid.UUID
    warnings: list[str]


# ---------------------------------------------------------------------------
# Catalog — search and browse
# ---------------------------------------------------------------------------


@router.get("/bazaar/agents")
async def list_catalog(
    current_user: Annotated[UserResult, Depends(get_current_user)],
    svc: Annotated[BazaarService, Depends(get_bazaar_service)],
    q: str = Query("", description="Full-text search query"),  # noqa: B008
    category_ids: list[uuid.UUID] = Query(default=[], alias="cat"),  # noqa: B008
    workspace_id: uuid.UUID = Query(..., description="Calling workspace for hired-flag"),  # noqa: B008
    page: int = Query(1, ge=1),  # noqa: B008
    size: int = Query(20, ge=1, le=100),  # noqa: B008
) -> dict[str, object]:
    return await svc.search_catalog(
        q,
        category_ids=category_ids,
        page=page,
        size=size,
        calling_workspace_id=workspace_id,
        user_id=current_user.id,
    )


@router.get("/bazaar/agents/{agent_id}")
async def get_agent_detail(
    agent_id: uuid.UUID,
    current_user: Annotated[UserResult, Depends(get_current_user)],
    svc: Annotated[BazaarService, Depends(get_bazaar_service)],
    workspace_id: uuid.UUID = Query(..., description="Calling workspace for hired-flag"),  # noqa: B008
) -> dict[str, object]:
    return await svc.get_agent_detail(
        agent_id, calling_workspace_id=workspace_id, user_id=current_user.id
    )


@router.get("/bazaar/categories")
async def list_categories(
    current_user: Annotated[UserResult, Depends(get_current_user)],
    svc: Annotated[BazaarService, Depends(get_bazaar_service)],
) -> list[dict[str, object]]:
    cats = await svc.list_categories()
    return [{"id": str(c.id), "name": c.name, "slug": c.slug} for c in cats]


@router.get("/bazaar/templates")
async def list_builtin_templates(
    current_user: Annotated[UserResult, Depends(get_current_user)],
) -> list[dict[str, object]]:
    """Built-in ready-to-use templates (virtual listings — a code registry, no DB).
    Personas stay server-side; the payload is card/chip metadata only. O(1)."""
    from sila.services.bazaar_templates import list_template_payloads

    return list_template_payloads()


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


@router.post("/bazaar/agents/{agent_id}/reviews", status_code=201)
async def submit_review(
    agent_id: uuid.UUID,
    body: ReviewIn,
    current_user: Annotated[UserResult, Depends(get_current_user)],
    svc: Annotated[BazaarService, Depends(get_bazaar_service)],
    background_tasks: BackgroundTasks,
    workspace_id: uuid.UUID = Query(..., description="Reviewer's workspace"),  # noqa: B008
) -> dict[str, object]:
    review = await svc.submit_review(
        agent_id=agent_id,
        workspace_id=workspace_id,
        author_id=current_user.id,
        rating=body.rating,
        body=body.body,
        background_tasks=background_tasks,
    )
    return {
        "id": str(review.id),
        "agent_id": str(review.agent_id),
        "rating": review.rating,
        "body": review.body,
        "created_at": review.created_at.isoformat(),
    }


@router.get("/bazaar/agents/{agent_id}/reviews")
async def list_reviews(
    agent_id: uuid.UUID,
    current_user: Annotated[UserResult, Depends(get_current_user)],
    svc: Annotated[BazaarService, Depends(get_bazaar_service)],
    page: int = Query(1, ge=1),  # noqa: B008
    size: int = Query(20, ge=1, le=100),  # noqa: B008
) -> dict[str, object]:
    return await svc.list_reviews(agent_id, page=page, size=size)


# ---------------------------------------------------------------------------
# Feedback — reactions, reports, trending
# ---------------------------------------------------------------------------


@router.get("/bazaar/trending")
async def get_trending(
    current_user: Annotated[UserResult, Depends(get_current_user)],
    svc: Annotated[BazaarService, Depends(get_bazaar_service)],
    workspace_id: uuid.UUID = Query(..., description="Calling workspace for hired-flag"),  # noqa: B008
) -> dict[str, object] | None:
    """The most-liked Published agent, or null when the Bazaar has no agents."""
    return await svc.get_trending(calling_workspace_id=workspace_id)


@router.post("/bazaar/agents/{agent_id}/reactions")
async def react_to_agent(
    agent_id: uuid.UUID,
    body: ReactionIn,
    current_user: Annotated[UserResult, Depends(get_current_user)],
    svc: Annotated[BazaarService, Depends(get_bazaar_service)],
    workspace_id: uuid.UUID = Query(..., description="Caller's workspace"),  # noqa: B008
) -> dict[str, object]:
    return await svc.react(
        workspace_id=workspace_id,
        agent_id=agent_id,
        user_id=current_user.id,
        reaction=body.reaction,
    )


@router.post("/bazaar/agents/{agent_id}/reports", status_code=201)
async def report_agent(
    agent_id: uuid.UUID,
    body: ReportIn,
    current_user: Annotated[UserResult, Depends(get_current_user)],
    svc: Annotated[BazaarService, Depends(get_bazaar_service)],
    background_tasks: BackgroundTasks,
    workspace_id: uuid.UUID = Query(..., description="Reporter's workspace"),  # noqa: B008
) -> dict[str, object]:
    report = await svc.report(
        workspace_id=workspace_id,
        agent_id=agent_id,
        reporter_id=current_user.id,
        reason=body.reason,
        background_tasks=background_tasks,
    )
    return {
        "id": str(report.id),
        "status": report.status,
        "created_at": report.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Hires — workspace-scoped
# ---------------------------------------------------------------------------


@router.post("/workspaces/{workspace_id}/hires", status_code=201)
async def hire_agent(
    workspace_id: uuid.UUID,
    agent_id: Annotated[uuid.UUID, Query()],  # noqa: B008
    auth: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.HIRER))],
    svc: Annotated[BazaarService, Depends(get_bazaar_service)],
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    current_user, _ = auth
    hire = await svc.hire(
        workspace_id=workspace_id,
        agent_id=agent_id,
        hired_by=current_user.id,
        background_tasks=background_tasks,
    )
    return {
        "id": str(hire.id),
        "workspace_id": str(hire.workspace_id),
        "agent_id": str(hire.agent_id),
        "hired_at": hire.hired_at.isoformat(),
    }


@router.post(
    "/workspaces/{workspace_id}/hires/{agent_id}/provision",
    status_code=201,
    response_model=ProvisionHireOut,
)
async def provision_hire(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,  # the SOURCE (published) agent
    body: ProvisionHireIn,
    background_tasks: BackgroundTasks,
    auth: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    n8n: Annotated[N8nService, Depends(get_n8n_service)],
) -> ProvisionHireOut:
    """Hire-with-your-own-accounts: provision the caller's OWN runnable copy of
    a published workflow agent — their destinations, their documents, their
    connector bindings — then submit it to the gatekeeper. BUILDER-gated: this
    creates an agent + workflow and mints capabilities (HIRER keeps the plain
    bookmark hire). The publisher's targets/docs never cross the boundary
    (whitelist merge; "included data" copies bytes into a hirer-owned KB)."""
    user, _role = auth
    source = await BazaarRepository(db).get_published(agent_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found or not published"
        )
    if source.kind != "workflow" or not source.config:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This agent doesn't support setup-based hiring — use the regular hire.",
        )

    agent, hire, warnings = await hire_provisioning.provision_hire(
        db=db,
        n8n=n8n,
        agent_service=AgentService(db, DifyAdapter()),
        source=source,
        hirer_user_id=user.id,
        hirer_workspace_id=workspace_id,
        destinations=[d.model_dump(exclude_none=True) for d in body.destinations],
        schedule=(
            {"cron": body.schedule.cron, "timezone": body.schedule.timezone}
            if body.schedule
            else None
        ),
        kb_id=body.kb_id,
        name=body.name,
    )
    # Snapshot BEFORE submit_for_review commits (it expires ORM objects — the
    # same ordering documented on the register-workflow endpoint).
    agent_out = AgentOut.from_orm(agent)
    run = await VettingService(db).submit_for_review(
        agent=agent, actor_id=user.id, background_tasks=background_tasks
    )
    return ProvisionHireOut(
        agent=agent_out, vetting_run_id=run.id, hire_id=hire.id, warnings=warnings
    )


@router.get("/workspaces/{workspace_id}/hires")
async def list_hires(
    workspace_id: uuid.UUID,
    auth: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    svc: Annotated[BazaarService, Depends(get_bazaar_service)],
    page: int = Query(1, ge=1),  # noqa: B008
    size: int = Query(20, ge=1, le=100),  # noqa: B008
) -> dict[str, object]:
    return await svc.list_hires(workspace_id, page=page, size=size)


@router.delete("/workspaces/{workspace_id}/hires/{agent_id}", status_code=204)
async def unhire_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    auth: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.HIRER))],
    svc: Annotated[BazaarService, Depends(get_bazaar_service)],
    background_tasks: BackgroundTasks,
) -> None:
    current_user, _ = auth
    await svc.unhire(
        workspace_id=workspace_id,
        agent_id=agent_id,
        unhired_by=current_user.id,
        background_tasks=background_tasks,
    )
