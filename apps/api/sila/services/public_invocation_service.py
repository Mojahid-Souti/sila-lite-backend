"""Public agent invocation — the external-callers hot path.

Flow:
  1. ApiCaller already resolved by `get_api_caller` dependency
  2. Rate-limit check (returns 429 with Retry-After on bust)
  3. Resolve agent in the API key's workspace
  4. Gate: agent must be status='published' AND vetting_status='approved'
  5. Call AgentRuntime.invoke (Dify adapter)
  6. Record `api_request_log` row (always, even on failure)
  7. Audit log: action=public.invoke

Webhook fan-out (agent.invoked / agent.failed) happens via WebhookService —
we enqueue a delivery row; the long-running worker actually sends.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.agent_runtime.base import AgentRuntime
from sila.adapters.agent_runtime.n8n_workflow import N8nWorkflowRuntime
from sila.core.constants import Action, AgentKind, AgentStatus, Outcome, ResourceType
from sila.core.cost_telemetry import cost_metadata, usage_from_dify
from sila.db.repositories.agent import AgentRepository
from sila.db.repositories.agent_version import AgentVersionRepository
from sila.db.repositories.audit import AuditRepository
from sila.services.rate_limit_service import RateLimitService


@dataclass(frozen=True)
class InvocationResult:
    answer: str
    usage: dict[str, Any]
    latency_ms: int


class PublicInvocationService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        runtime: AgentRuntime,
        rate_limiter: RateLimitService,
    ) -> None:
        self._db = db
        self._agents = AgentRepository(db)
        self._versions = AgentVersionRepository(db)
        self._audit = AuditRepository(db)
        self._runtime = runtime
        self._workflow_runtime = N8nWorkflowRuntime()
        self._rate_limiter = rate_limiter

    async def invoke(
        self,
        *,
        agent_id: uuid.UUID,
        workspace_id: uuid.UUID,
        api_key_id: uuid.UUID,
        query: str,
    ) -> InvocationResult:
        rl = await self._rate_limiter.check(api_key_id)
        if not rl.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded: {rl.requests_last_minute}/min, "
                    f"{rl.requests_today}/day. Retry in {rl.retry_after_seconds}s."
                ),
                headers={
                    "Retry-After": str(rl.retry_after_seconds),
                    "X-RateLimit-Limit": str(rl.limit_minute),
                    "X-RateLimit-Remaining": str(max(0, rl.limit_minute - rl.requests_last_minute)),
                },
            )

        agent = await self._agents.get(agent_id, workspace_id)
        if not agent:
            await self._rate_limiter.record(
                api_key_id=api_key_id,
                workspace_id=workspace_id,
                agent_id=None,
                endpoint="invoke",
                status_code=404,
                latency_ms=0,
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

        # Phase 6 + 2 gate: only Published + Approved agents are externally callable.
        if agent.status != AgentStatus.PUBLISHED or agent.vetting_status != "approved":
            await self._rate_limiter.record(
                api_key_id=api_key_id,
                workspace_id=workspace_id,
                agent_id=agent.id,
                endpoint="invoke",
                status_code=409,
                latency_ms=0,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Agent is not externally callable: "
                    f"status={agent.status!r}, vetting_status={agent.vetting_status!r}. "
                    "Required: status='published' and vetting_status='approved'."
                ),
            )

        # Resolve invocation inputs OUTSIDE the failure-translation block so
        # config problems surface as clean 409s, not generic 502s.
        workflow_definition: dict | None = None  # type: ignore[type-arg]
        if agent.kind == AgentKind.WORKFLOW:
            version = await self._versions.get(agent.id, agent.version)
            workflow_definition = version.dify_dsl if version else None
            if not workflow_definition:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Agent has no workflow definition",
                )
        elif not agent.dify_app_id:
            # Chat agents register with the runtime lazily on first sandbox
            # test; a published agent should always have an app id by then.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Agent is not registered with the runtime yet — run a sandbox test first",
            )

        start = time.perf_counter()
        outcome = Outcome.SUCCESS
        try:
            if workflow_definition is not None:
                result = await self._workflow_runtime.invoke_workflow(
                    workflow_definition, {"query": query}
                )
            else:
                result = await self._runtime.invoke_sandbox(
                    app_id=agent.dify_app_id or "",
                    query=query,
                    user_id=f"public:{api_key_id}",
                )
            answer = str(result.get("answer", ""))
            usage = dict(result.get("usage", {}))
        except Exception as exc:  # noqa: BLE001 — translate to 502, record audit, re-raise
            outcome = Outcome.FAILURE
            latency_ms = int((time.perf_counter() - start) * 1000)
            await self._rate_limiter.record(
                api_key_id=api_key_id,
                workspace_id=workspace_id,
                agent_id=agent.id,
                endpoint="invoke",
                status_code=502,
                latency_ms=latency_ms,
            )
            await self._audit.record(
                action=Action.PUBLIC_INVOKE,
                outcome=outcome,
                actor_user_id=None,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.AGENT,
                resource_id=agent.id,
                metadata={"api_key_id": str(api_key_id), "error": str(exc)[:200]},
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Agent runtime failed: {exc}",
            ) from exc

        latency_ms = int((time.perf_counter() - start) * 1000)

        await self._rate_limiter.record(
            api_key_id=api_key_id,
            workspace_id=workspace_id,
            agent_id=agent.id,
            endpoint="invoke",
            status_code=200,
            latency_ms=latency_ms,
        )
        # G1: production invocations now carry the SAME cost telemetry as sandbox
        # tests (tokens split in/out, prefill/decode seconds, agent id). Before
        # this, only `agent.test` rows had tokens, so measured volume — and every
        # cost figure derived from it — was a fraction of real volume.
        engine = "n8n" if workflow_definition is not None else "dify"
        block = (
            usage_from_dify({"usage": usage}) if engine == "dify" else {**usage, "engine": "n8n"}
        )
        meta = (
            cost_metadata(
                block,
                agent_id=agent.id,
                latency_ms=latency_ms,
                engine=engine,
                surface="public_api",
            )
            or {}
        )
        meta["api_key_id"] = str(api_key_id)
        await self._audit.record(
            action=Action.PUBLIC_INVOKE,
            outcome=outcome,
            actor_user_id=None,
            actor_workspace_id=workspace_id,
            resource_type=ResourceType.AGENT,
            resource_id=agent.id,
            metadata=meta,
        )
        return InvocationResult(answer=answer, usage=usage, latency_ms=latency_ms)
