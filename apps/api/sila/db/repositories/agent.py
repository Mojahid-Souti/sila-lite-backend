"""AgentRepository — all agent DB queries.

Every query is workspace-scoped to prevent cross-tenant leakage.
Indexes used:
  - ix_agents_workspace_status_updated for list (O(log n))
  - primary key for get-by-id (O(1))
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import Agent, AuditLog


class AgentRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        description: str,
        owner_id: uuid.UUID,
        tags: list[str],
        classification: str,
        language: str,
        dify_dsl: dict,  # type: ignore[type-arg]
        kind: str = "chat",
        n8n_workflow_id: str | None = None,
        config: dict | None = None,  # type: ignore[type-arg]
    ) -> Agent:
        """Insert new agent row. O(1)."""
        agent = Agent(
            workspace_id=workspace_id,
            name=name,
            description=description,
            owner_id=owner_id,
            tags=tags,
            classification=classification,
            language=language,
            status="draft",
            version=1,
            kind=kind,
            n8n_workflow_id=n8n_workflow_id,
            config=config,
        )
        self._db.add(agent)
        await self._db.flush()
        return agent

    async def get(self, agent_id: uuid.UUID, workspace_id: uuid.UUID) -> Agent | None:
        """Fetch agent by PK, scoped to workspace. O(1) via primary key."""
        result = await self._db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.workspace_id == workspace_id)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        workspace_id: uuid.UUID,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[Agent], int]:
        """List agents in workspace with optional status filter, paginated.

        O(log n) via ix_agents_workspace_status_updated.
        Returns (items, total_count).
        """
        base = select(Agent).where(Agent.workspace_id == workspace_id)
        if status:
            base = base.where(Agent.status == status)

        count_q = select(func.count()).select_from(base.subquery())
        total = (await self._db.execute(count_q)).scalar_one()

        items_q = base.order_by(Agent.updated_at.desc()).offset((page - 1) * size).limit(size)
        rows = (await self._db.execute(items_q)).scalars().all()
        return list(rows), total

    async def update(self, agent: Agent, **fields: object) -> Agent:
        """Apply field updates to an agent row. O(1)."""
        for k, v in fields.items():
            setattr(agent, k, v)
        await self._db.flush()
        return agent

    async def count_sandbox_uses_today(self, user_id: uuid.UUID, workspace_id: uuid.UUID) -> int:
        """Count agent.test audit rows for user in current UTC day. O(log n) via audit_log index."""
        result = await self._db.execute(
            select(func.count()).where(
                AuditLog.actor_user_id == user_id,
                AuditLog.actor_workspace_id == workspace_id,
                AuditLog.action == "agent.test",
                AuditLog.outcome == "success",
                func.date(AuditLog.timestamp) == func.current_date(),
            )
        )
        return result.scalar_one()
