"""Phase 6 — Security Gatekeeper endpoints.

Mounted under /v1/workspaces/{wid}/agents/{aid}/...
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.auth.base import UserResult
from sila.api.deps import check_opa, require_ai_enabled, require_workspace_role
from sila.core.constants import Action, Role
from sila.db.models import Agent, VettingFinding, VettingRun
from sila.db.repositories.agent import AgentRepository
from sila.db.repositories.membership import MembershipRepository
from sila.db.repositories.vetting import VettingRepository
from sila.db.session import get_db
from sila.services.vetting_service import VettingService

router = APIRouter(tags=["vetting"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DecisionRequest(BaseModel):
    note: str | None = None


class VettingRunOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    workspace_id: uuid.UUID
    classification: str
    status: str
    outcome: str | None
    triggered_by: uuid.UUID
    approved_by: uuid.UUID | None
    approval_decision: str | None
    approval_note: str | None
    finding_summary: dict[str, Any]
    started_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_model(cls, r: VettingRun) -> VettingRunOut:
        return cls(
            id=r.id,
            agent_id=r.agent_id,
            workspace_id=r.workspace_id,
            classification=r.classification,
            status=r.status,
            outcome=r.outcome,
            triggered_by=r.triggered_by,
            approved_by=r.approved_by,
            approval_decision=r.approval_decision,
            approval_note=r.approval_note,
            finding_summary=r.finding_summary or {},
            started_at=r.started_at,
            completed_at=r.completed_at,
        )


class VettingFindingOut(BaseModel):
    id: uuid.UUID
    finding_type: str
    severity: str
    location: str
    matched_preview: str
    matched_full: str | None
    metadata: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_model(cls, f: VettingFinding) -> VettingFindingOut:
        return cls(
            id=f.id,
            finding_type=f.finding_type,
            severity=f.severity,
            location=f.location,
            matched_preview=f.matched_preview,
            matched_full=f.matched_full,
            metadata=f.finding_metadata or {},
            created_at=f.created_at,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_agent(db: AsyncSession, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> Agent:
    agent = await AgentRepository(db).get(agent_id, workspace_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


async def _user_is_admin(db: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Used to decide whether the caller can see full matched text in findings."""
    members = await MembershipRepository(db).list_for_workspace(workspace_id)
    return any(m.user_id == user_id and m.role == "admin" for m in members)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/workspaces/{workspace_id}/agents/{agent_id}/submit-for-review",
    response_model=VettingRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_ai_enabled)],
)
async def submit_for_review(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> VettingRunOut:
    current_user, role = caller
    if not await check_opa(Action.AGENT_SUBMIT_REVIEW, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    agent = await _load_agent(db, workspace_id, agent_id)
    svc = VettingService(db)
    run = await svc.submit_for_review(
        agent=agent, actor_id=current_user.id, background_tasks=background_tasks
    )
    return VettingRunOut.from_model(run)


@router.get(
    "/workspaces/{workspace_id}/agents/{agent_id}/vetting-runs",
    response_model=list[VettingRunOut],
)
async def list_vetting_runs(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[VettingRunOut]:
    await _load_agent(db, workspace_id, agent_id)
    runs = await VettingRepository(db).list_for_agent(agent_id)
    return [VettingRunOut.from_model(r) for r in runs]


@router.get(
    "/workspaces/{workspace_id}/agents/{agent_id}/vetting-runs/{run_id}",
    response_model=VettingRunOut,
)
async def get_vetting_run(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VettingRunOut:
    run = await VettingRepository(db).get_run(run_id)
    if not run or run.workspace_id != workspace_id or run.agent_id != agent_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vetting run not found")
    return VettingRunOut.from_model(run)


@router.get(
    "/workspaces/{workspace_id}/agents/{agent_id}/vetting-runs/{run_id}/findings",
    response_model=list[VettingFindingOut],
)
async def list_findings(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[VettingFindingOut]:
    current_user, _role = caller
    run = await VettingRepository(db).get_run(run_id)
    if not run or run.workspace_id != workspace_id or run.agent_id != agent_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vetting run not found")

    is_admin = await _user_is_admin(db, workspace_id, current_user.id)
    findings = await VettingRepository(db).list_findings(run_id, include_full=is_admin)
    return [VettingFindingOut.from_model(f) for f in findings]


# Approval / rejection roles. REVIEWER and BUILDER are intentionally parallel
# roles (not hierarchical) — the rank-based `require_workspace_role` would
# otherwise let a BUILDER satisfy "Role.REVIEWER" because BUILDER's rank is
# higher. CLAUDE.md §6 calls for separation of duties: "Builder cannot
# self-approve their own Restricted agent". So we gate the decision endpoints
# on explicit role membership in this set.
_APPROVAL_ROLES: frozenset[Role] = frozenset({Role.REVIEWER, Role.ADMIN})


def _require_approval_role(role: Role) -> None:
    if role not in _APPROVAL_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Vetting decisions require the Reviewer or Admin role. Builders "
                "cannot approve or reject their own agents (separation of duties)."
            ),
        )


async def _require_not_self_approval(
    db: AsyncSession, run_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """SoD: the approver cannot be the user who submitted the run for review."""
    run = await VettingRepository(db).get_run(run_id)
    if run and run.triggered_by == actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You submitted this run for review and cannot also approve it. "
                "Another reviewer or admin must make the decision."
            ),
        )


@router.post(
    "/workspaces/{workspace_id}/agents/{agent_id}/vetting-runs/{run_id}/approve",
    response_model=VettingRunOut,
)
async def approve(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    body: DecisionRequest,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VettingRunOut:
    current_user, role = caller
    _require_approval_role(role)
    if not await check_opa(Action.AGENT_APPROVE, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    await _require_not_self_approval(db, run_id, current_user.id)
    svc = VettingService(db)
    run = await svc.approve(
        run_id=run_id, workspace_id=workspace_id, actor_id=current_user.id, note=body.note
    )
    return VettingRunOut.from_model(run)


@router.post(
    "/workspaces/{workspace_id}/agents/{agent_id}/vetting-runs/{run_id}/reject",
    response_model=VettingRunOut,
)
async def reject(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    body: DecisionRequest,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VettingRunOut:
    current_user, role = caller
    _require_approval_role(role)
    if not await check_opa(Action.AGENT_REJECT, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    svc = VettingService(db)
    run = await svc.reject(
        run_id=run_id, workspace_id=workspace_id, actor_id=current_user.id, note=body.note
    )
    return VettingRunOut.from_model(run)
