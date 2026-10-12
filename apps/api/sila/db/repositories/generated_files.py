"""Short-lived generated-file downloads (capability URLs, 24h TTL)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import GeneratedFile

_TTL = timedelta(hours=24)


class GeneratedFileRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        filename: str,
        content_type: str,
        file_bytes: bytes,
    ) -> GeneratedFile:
        # Prune expired rows opportunistically — no cron needed at POC scale.
        await self._db.execute(
            delete(GeneratedFile).where(GeneratedFile.created_at < datetime.now(UTC) - _TTL)
        )
        row = GeneratedFile(
            workspace_id=workspace_id,
            user_id=user_id,
            filename=filename,
            content_type=content_type,
            file_bytes=file_bytes,
        )
        self._db.add(row)
        await self._db.flush()
        return row

    async def get_live(self, file_id: uuid.UUID) -> GeneratedFile | None:
        row = await self._db.get(GeneratedFile, file_id)
        if row is None:
            return None
        created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
        return row if datetime.now(UTC) - created <= _TTL else None
