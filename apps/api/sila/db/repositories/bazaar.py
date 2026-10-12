"""BazaarRepository — catalog queries for published agents.

Provides SQL-based catalog listing (Meilisearch is the primary search path;
this is the fallback and the source for Meilisearch sync).

Indexes used:
  - ix_agents_workspace_status_updated: (workspace_id, status, updated_at DESC) — O(log n)
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import Agent, AgentCategory


class BazaarRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_published(
        self,
        *,
        category_id: uuid.UUID | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[Agent], int]:
        """List published agents, newest first.

        Optionally filter by category. O(log n) via composite index on status + updated_at.
        n = total published agents.
        """
        base = select(Agent).where(Agent.status == "published")
        if category_id is not None:
            base = base.join(
                AgentCategory,
                (AgentCategory.agent_id == Agent.id) & (AgentCategory.category_id == category_id),
            )

        total_result = await self._db.execute(select(func.count()).select_from(base.subquery()))
        total = total_result.scalar_one()
        rows = await self._db.execute(
            base.order_by(Agent.updated_at.desc()).offset((page - 1) * size).limit(size)
        )
        return list(rows.scalars().all()), total

    async def get_published(self, agent_id: uuid.UUID) -> Agent | None:
        """Fetch a single published agent by PK. O(1)."""
        result = await self._db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.status == "published")
        )
        return result.scalar_one_or_none()

    async def all_published(self) -> list[Agent]:
        """Return all published agents for backfill indexing. O(n) — offline only."""
        rows = await self._db.execute(select(Agent).where(Agent.status == "published"))
        return list(rows.scalars().all())
