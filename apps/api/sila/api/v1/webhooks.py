"""Webhook subscription management (admin-only).

Mounted at /v1/workspaces/{wid}/webhooks. Authenticated via JWT (admin role),
NOT API key — API keys are for outbound consumers; subscriptions are
workspace-admin concerns.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.auth.base import UserResult
from sila.api.deps import check_opa, require_workspace_role
from sila.core.constants import Action, Role
from sila.db.models import WebhookSubscription
from sila.db.session import get_db
from sila.services.webhook_service import ALLOWED_EVENTS, WebhookService

router = APIRouter(prefix="/workspaces/{workspace_id}/webhooks", tags=["webhooks"])


class CreateWebhookRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    url: str
    events: list[str] = Field(min_length=1)


class WebhookOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    url: str
    events: list[str]
    secret_prefix: str
    status: str
    created_at: datetime

    @classmethod
    def from_model(cls, s: WebhookSubscription) -> WebhookOut:
        return cls(
            id=s.id,
            workspace_id=s.workspace_id,
            name=s.name,
            url=s.url,
            events=list(s.events),
            secret_prefix=s.secret_prefix,
            status=s.status,
            created_at=s.created_at,
        )


class WebhookCreatedOut(WebhookOut):
    """Returned once on creation; `secret` is the plaintext signing key."""

    secret: str


@router.post("", response_model=WebhookCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    workspace_id: uuid.UUID,
    body: CreateWebhookRequest,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookCreatedOut:
    current_user, role = caller
    if not await check_opa(Action.WEBHOOK_CREATE, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    svc = WebhookService(db)
    created = await svc.create(
        workspace_id=workspace_id,
        name=body.name,
        url=body.url,
        events=body.events,
        actor_id=current_user.id,
    )
    return WebhookCreatedOut(
        **WebhookOut.from_model(created.subscription).model_dump(),
        secret=created.secret,
    )


@router.get("", response_model=list[WebhookOut])
async def list_webhooks(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[WebhookOut]:
    svc = WebhookService(db)
    subs = await svc.list_for_workspace(workspace_id)
    return [WebhookOut.from_model(s) for s in subs]


@router.delete("/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    workspace_id: uuid.UUID,
    subscription_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    current_user, role = caller
    if not await check_opa(Action.WEBHOOK_DELETE, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    svc = WebhookService(db)
    await svc.delete(
        subscription_id=subscription_id, workspace_id=workspace_id, actor_id=current_user.id
    )


@router.get("/events", response_model=list[str])
async def list_event_types(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
) -> list[str]:
    """List the event names a subscription may opt into."""
    return sorted(ALLOWED_EVENTS)
