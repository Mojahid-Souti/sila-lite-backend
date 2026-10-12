"""HireRepository — workspace hire records.

All queries are workspace-scoped.
Indexes used:
  - ix_hires_workspace_hired_at for list  (O(log n))
  - uq_hire unique constraint for exists check (O(1))
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import Hire


class HireRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def hire(
        self,
        *,
        workspace_id: uuid.UUID,
        agent_id: uuid.UUID,
        hired_by: uuid.UUID,
    ) -> Hire:
        """Insert hire row. Idempotent: if already hired, returns existing row. O(1)."""
        existing = await self.get(workspace_id=workspace_id, agent_id=agent_id)
        if existing:
            return existing
        hire = Hire(workspace_id=workspace_id, agent_id=agent_id, hired_by=hired_by)
        self._db.add(hire)
        await self._db.flush()
        return hire

    async def get(self, *, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> Hire | None:
        """Fetch hire by workspace + agent. O(1) via unique index."""
        result = await self._db.execute(
            select(Hire).where(Hire.workspace_id == workspace_id, Hire.agent_id == agent_id)
        )
        return result.scalar_one_or_none()

    async def unhire(self, *, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
        """Delete hire row. Returns True if deleted, False if not found. O(1)."""
        hire = await self.get(workspace_id=workspace_id, agent_id=agent_id)
        if not hire:
            return False
        await self._db.delete(hire)
        await self._db.flush()
        return True

    async def list(
        self,
        workspace_id: uuid.UUID,
        *,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[Hire], int]:
        """List hires for a workspace, most-recently-hired first. O(log n)."""
        from sqlalchemy import func

        base = select(Hire).where(Hire.workspace_id == workspace_id)
        total_result = await self._db.execute(select(func.count()).select_from(base.subquery()))
        total = total_result.scalar_one()

        rows = await self._db.execute(
            base.order_by(Hire.hired_at.desc()).offset((page - 1) * size).limit(size)
        )
        return list(rows.scalars().all()), total

    async def is_hired(self, *, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
        """Single EXISTS check — O(1) via unique index."""
        result = await self._db.execute(
            select(exists().where(Hire.workspace_id == workspace_id, Hire.agent_id == agent_id))
        )
        return bool(result.scalar())

    async def hired_agent_ids(self, workspace_id: uuid.UUID) -> set[uuid.UUID]:
        """Return all agent IDs hired by a workspace. O(k) where k = hires in workspace."""
        rows = await self._db.execute(
            select(Hire.agent_id).where(Hire.workspace_id == workspace_id)
        )
        return {r for r in rows.scalars().all()}
