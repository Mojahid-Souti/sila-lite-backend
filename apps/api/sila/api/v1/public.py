"""Public API surface — external callers via Bearer API key.

Mounted at /v1. Auth is `Authorization: Bearer wk_...`. Internal endpoints
use the same /v1 prefix but a different auth dep (`get_current_user`).

The OpenAPI generator picks up everything tagged `public` and exposes it
to SDK consumers. Internal/admin endpoints use other tags and are filtered
by the public OpenAPI proxy in main.py.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.agent_runtime.dify import DifyAdapter
from sila.adapters.auth.api_key import ApiCaller, get_api_caller
from sila.core.config import settings
from sila.db.session import get_db
from sila.services.public_invocation_service import PublicInvocationService
from sila.services.rate_limit_service import RateLimitService

router = APIRouter(prefix="/agents", tags=["public"])


class InvokeRequest(BaseModel):
    query: str = Field(min_length=1, max_length=32_000)


class InvokeResponse(BaseModel):
    agent_id: uuid.UUID
    answer: str
    usage: dict[str, Any]
    latency_ms: int


def _build_invocation_service(db: AsyncSession) -> PublicInvocationService:
    limiter = RateLimitService(
        db,
        per_minute=settings.api_rate_limit_per_minute,
        per_day=settings.api_rate_limit_per_day,
    )
    return PublicInvocationService(db, runtime=DifyAdapter(), rate_limiter=limiter)


@router.post(
    "/{agent_id}/invoke",
    response_model=InvokeResponse,
    summary="Invoke an agent",
    description=(
        "Synchronously invokes a published, approved agent. The agent's "
        "workspace is inferred from the API key. Rate-limited per key."
    ),
)
async def invoke_agent(
    agent_id: uuid.UUID,
    body: InvokeRequest,
    caller: Annotated[ApiCaller, Depends(get_api_caller)],
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> InvokeResponse:
    svc = _build_invocation_service(db)
    result = await svc.invoke(
        agent_id=agent_id,
        workspace_id=caller.workspace_id,
        api_key_id=caller.api_key_id,
        query=body.query,
    )

    # Fan out webhook events asynchronously. We import inline to avoid a
    # circular import on app boot (WebhookService imports the session).
    async def _enqueue_invoked() -> None:
        from sila.db.session import AsyncSessionLocal
        from sila.services.webhook_service import WebhookService

        async with AsyncSessionLocal.begin() as bg_db:
            await WebhookService(bg_db).fan_out(
                workspace_id=caller.workspace_id,
                event="agent.invoked",
                data={
                    "agent_id": str(agent_id),
                    "workspace_id": str(caller.workspace_id),
                    "latency_ms": result.latency_ms,
                },
            )

    background_tasks.add_task(_enqueue_invoked)

    return InvokeResponse(
        agent_id=agent_id,
        answer=result.answer,
        usage=result.usage,
        latency_ms=result.latency_ms,
    )
