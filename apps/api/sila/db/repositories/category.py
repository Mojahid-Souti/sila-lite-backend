"""CategoryRepository — agent category taxonomy.

Categories are global (not workspace-scoped). The list is small (<100)
and cached at the service layer, so all operations are effectively O(1).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import AgentCategory, Category


class CategoryRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_all(self) -> list[Category]:
        """Return all categories ordered by name. O(n) where n < 100."""
        rows = await self._db.execute(select(Category).order_by(Category.name))
        return list(rows.scalars().all())

    async def get(self, category_id: uuid.UUID) -> Category | None:
        result = await self._db.execute(select(Category).where(Category.id == category_id))
        return result.scalar_one_or_none()

    async def set_agent_categories(
        self, agent_id: uuid.UUID, category_ids: list[uuid.UUID]
    ) -> None:
        """Replace all categories for an agent atomically. O(k) where k = category count."""
        await self._db.execute(delete(AgentCategory).where(AgentCategory.agent_id == agent_id))
        for cid in category_ids:
            self._db.add(AgentCategory(agent_id=agent_id, category_id=cid))
        await self._db.flush()

    async def get_agent_category_ids(self, agent_id: uuid.UUID) -> list[uuid.UUID]:
        """Return category IDs for an agent. O(k)."""
        rows = await self._db.execute(
            select(AgentCategory.category_id).where(AgentCategory.agent_id == agent_id)
        )
        return list(rows.scalars().all())
