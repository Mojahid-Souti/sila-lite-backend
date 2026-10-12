"""AgentVersionRepository — versioned DSL snapshots.

Queries use ix_agent_versions_agent_version index (agent_id, version_num DESC).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import AgentVersion


class AgentVersionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        *,
        agent_id: uuid.UUID,
        version_num: int,
        name: str,
        description: str,
        dify_dsl: dict,  # type: ignore[type-arg]
        changed_by: uuid.UUID,
        change_note: str = "",
    ) -> AgentVersion:
        """Insert a new version snapshot. O(1)."""
        ver = AgentVersion(
            agent_id=agent_id,
            version_num=version_num,
            name=name,
            description=description,
            dify_dsl=dify_dsl,
            changed_by=changed_by,
            change_note=change_note,
        )
        self._db.add(ver)
        await self._db.flush()
        return ver

    async def get(self, agent_id: uuid.UUID, version_num: int) -> AgentVersion | None:
        """Fetch specific version. O(1) via unique index."""
        result = await self._db.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent_id,
                AgentVersion.version_num == version_num,
            )
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        agent_id: uuid.UUID,
        page: int = 1,
        size: int = 20,
    ) -> list[AgentVersion]:
        """List versions newest-first, paginated. O(k) where k = versions per agent (<50)."""
        result = await self._db.execute(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version_num.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(result.scalars().all())

    async def next_version_num(self, agent_id: uuid.UUID) -> int:
        """Return current max version_num + 1. O(1) via index."""
        from sqlalchemy import func

        result = await self._db.execute(
            select(func.max(AgentVersion.version_num)).where(AgentVersion.agent_id == agent_id)
        )
        current = result.scalar_one_or_none()
        return (current or 0) + 1
