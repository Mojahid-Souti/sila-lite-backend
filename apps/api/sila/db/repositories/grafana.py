"""Data access for the per-workspace Grafana connection.

One row per workspace (unique constraint). Workspace-scoped — callers pass
workspace_id to enforce tenant isolation alongside RLS.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import GrafanaConnection


class GrafanaConnectionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_for_workspace(self, workspace_id: uuid.UUID) -> GrafanaConnection | None:
        result = await self._db.execute(
            select(GrafanaConnection).where(GrafanaConnection.workspace_id == workspace_id)
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        workspace_id: uuid.UUID,
        base_url: str,
        token_encrypted: bytes,
        registered_by: uuid.UUID,
    ) -> GrafanaConnection:
        """Create or replace the workspace's Grafana connection (one per workspace)."""
        existing = await self.get_for_workspace(workspace_id)
        if existing is not None:
            existing.base_url = base_url
            existing.token_encrypted = token_encrypted
            existing.registered_by = registered_by
            await self._db.flush()
            return existing
        conn = GrafanaConnection(
            workspace_id=workspace_id,
            base_url=base_url,
            token_encrypted=token_encrypted,
            registered_by=registered_by,
        )
        self._db.add(conn)
        await self._db.flush()
        return conn

    async def delete_for_workspace(self, workspace_id: uuid.UUID) -> bool:
        existing = await self.get_for_workspace(workspace_id)
        if existing is None:
            return False
        await self._db.delete(existing)
        await self._db.flush()
        return True
