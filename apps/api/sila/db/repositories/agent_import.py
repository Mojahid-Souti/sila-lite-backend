"""AgentImportRepository — audit trail of every import attempt."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import AgentImport


class AgentImportRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def record(
        self,
        *,
        workspace_id: uuid.UUID,
        imported_by: uuid.UUID,
        source: str,
        status: str,
        agent_id: uuid.UUID | None = None,
        filename: str | None = None,
        raw_yaml: str | None = None,
        error_msg: str | None = None,
    ) -> AgentImport:
        """Append-only import audit record. O(1)."""
        entry = AgentImport(
            workspace_id=workspace_id,
            agent_id=agent_id,
            imported_by=imported_by,
            source=source,
            filename=filename,
            # Truncate to 10_000 chars per schema design
            raw_yaml=raw_yaml[:10_000] if raw_yaml else None,
            status=status,
            error_msg=error_msg,
        )
        self._db.add(entry)
        await self._db.flush()
        return entry
