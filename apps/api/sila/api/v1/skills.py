"""Open-source skills in the Bazaar (Phase 18).

Browse (any signed-in user): GET /bazaar/skills, GET /bazaar/skills/{id}.
Workspace-scoped (role via membership, action via OPA):
  GET  /workspaces/{wid}/skills/catalog     search the upstream catalog  (skill.import)
  POST /workspaces/{wid}/skills/import      import + vet one skill       (skill.import)
  GET  /workspaces/{wid}/skills             this workspace's imports     (member)
  POST /workspaces/{wid}/skills/{sid}/approve | reject | rescan | publish | unpublish
                                                                            (Reviewer+)
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.auth.base import UserResult
from sila.adapters.skills.skills_library import SkillsLibraryAdapter
from sila.api.deps import check_opa, get_current_user, require_ai_enabled, require_workspace_role
from sila.core.config import settings
from sila.core.constants import Action, Role
from sila.db.repositories.membership import MembershipRepository
from sila.db.repositories.skills import SkillFilters
from sila.db.session import get_db
from sila.services.skills.service import SkillService, skill_payload

router = APIRouter()

Member = Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))]


def get_skill_service(db: Annotated[AsyncSession, Depends(get_db)]) -> SkillService:
    source = SkillsLibraryAdapter(
        catalog_url=settings.skills_catalog_url,
        github_api_url=settings.skills_github_api_url,
        raw_url=settings.skills_raw_url,
        allowed_hosts=settings.skills_allowed_hosts.split(","),
        github_token=settings.github_token,
        catalog_max_bytes=settings.skills_catalog_max_bytes,
        skill_md_max_bytes=settings.skill_md_max_bytes,
        timeout_s=settings.skills_fetch_timeout_s,
    )
    return SkillService(db, source)


Service = Annotated[SkillService, Depends(get_skill_service)]


class ImportIn(BaseModel):
    source: str = Field(..., min_length=3, max_length=200)
    skill_key: str = Field(..., min_length=1, max_length=200)


async def _allow(action: Action, role: Role) -> None:
    if not await check_opa(action, role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")


# --- browse ---------------------------------------------------------------------


@router.get("/bazaar/skills")
async def list_skills(
    _user: Annotated[UserResult, Depends(get_current_user)],
    svc: Service,
    q: str = Query("", max_length=200),  # noqa: B008
    category: str | None = Query(None, max_length=40),  # noqa: B008
    instructions_only: bool = False,
    licensed_only: bool = False,
    sort: Literal["popular", "newest"] = "popular",
    page: int = Query(1, ge=1),  # noqa: B008
    size: int = Query(20, ge=1, le=100),  # noqa: B008
) -> dict[str, object]:
    filters = SkillFilters(
        query=q,
        category=category,
        instructions_only=instructions_only,
        licensed_only=licensed_only,
        sort=sort,
    )
    return await svc.list_published(filters, page=page, size=size)


@router.get("/bazaar/skills/{skill_id}")
async def get_skill(
    skill_id: uuid.UUID,
    user: Annotated[UserResult, Depends(get_current_user)],
    svc: Service,
    db: Annotated[AsyncSession, Depends(get_db)],
    workspace_id: uuid.UUID | None = None,
) -> dict[str, object]:
    """A published skill with its instructions — or, for a member of the
    importing workspace, one of its drafts."""
    member_of: set[uuid.UUID] = set()
    if workspace_id is not None and await MembershipRepository(db).get(workspace_id, user.id):
        member_of.add(workspace_id)
    return skill_payload(await svc.get_visible(skill_id, member_of), with_body=True)


# --- a workspace's imports ------------------------------------------------------


@router.get("/workspaces/{workspace_id}/skills/catalog")
async def search_catalog(
    workspace_id: uuid.UUID,
    auth: Member,
    svc: Service,
    q: str = Query("", max_length=200),  # noqa: B008
    page: int = Query(1, ge=1),  # noqa: B008
    size: int = Query(20, ge=1, le=100),  # noqa: B008
) -> dict[str, object]:
    await _allow(Action.SKILL_IMPORT, auth[1])
    return await svc.search_catalog(q, page=page, size=size)


@router.post(
    "/workspaces/{workspace_id}/skills/import",
    status_code=201,
    dependencies=[Depends(require_ai_enabled)],
)
async def import_skill(
    workspace_id: uuid.UUID, body: ImportIn, auth: Member, svc: Service
) -> dict[str, object]:
    await _allow(Action.SKILL_IMPORT, auth[1])
    skill = await svc.import_skill(
        workspace_id=workspace_id,
        user_id=auth[0].id,
        source=body.source.strip(),
        skill_key=body.skill_key.strip(),
    )
    return skill_payload(skill, with_body=True)


@router.get("/workspaces/{workspace_id}/skills")
async def list_workspace_skills(
    workspace_id: uuid.UUID,
    _auth: Member,
    svc: Service,
    page: int = Query(1, ge=1),  # noqa: B008
    size: int = Query(20, ge=1, le=100),  # noqa: B008
) -> dict[str, object]:
    skills = await svc.list_workspace(workspace_id, page=page, size=size)
    return {"items": [skill_payload(s) for s in skills], "page": page, "size": size}


@router.delete(
    "/workspaces/{workspace_id}/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove(workspace_id: uuid.UUID, skill_id: uuid.UUID, auth: Member, svc: Service) -> None:
    user, role = auth
    await _allow(Action.SKILL_REMOVE, role)
    await svc.remove(workspace_id, skill_id, user.id)


@router.post("/workspaces/{workspace_id}/skills/{skill_id}/{decision}")
async def decide(
    workspace_id: uuid.UUID,
    skill_id: uuid.UUID,
    decision: Literal["approve", "reject", "rescan", "publish", "unpublish"],
    auth: Member,
    svc: Service,
) -> dict[str, object]:
    user, role = auth
    if decision == "approve":
        await _allow(Action.SKILL_APPROVE, role)
        skill = await svc.approve(workspace_id, skill_id, user.id)
    elif decision == "reject":
        await _allow(Action.SKILL_REJECT, role)
        skill = await svc.reject(workspace_id, skill_id, user.id)
    elif decision == "rescan":
        await _allow(Action.SKILL_RESCAN, role)
        await require_ai_enabled()
        skill = await svc.rescan(workspace_id, skill_id, user.id)
    else:
        publish = decision == "publish"
        await _allow(Action.SKILL_PUBLISH if publish else Action.SKILL_UNPUBLISH, role)
        skill = await svc.set_published(workspace_id, skill_id, user.id, published=publish)
    return skill_payload(skill)
