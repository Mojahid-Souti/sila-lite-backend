"""Data access for workspace slide templates (corporate .pptx files)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import PptxTemplate


class PptxTemplateRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        file_bytes: bytes,
        uploaded_by: uuid.UUID,
    ) -> PptxTemplate:
        tpl = PptxTemplate(
            workspace_id=workspace_id,
            name=name,
            file_bytes=file_bytes,
            file_size=len(file_bytes),
            uploaded_by=uploaded_by,
        )
        self._db.add(tpl)
        await self._db.flush()
        return tpl

    async def list_for_workspace(self, workspace_id: uuid.UUID) -> list[PptxTemplate]:
        """Names/sizes for pickers — file bytes are deferred until a build needs them."""
        result = await self._db.execute(
            select(PptxTemplate)
            .where(PptxTemplate.workspace_id == workspace_id)
            .order_by(PptxTemplate.created_at.desc())
        )
        return list(result.scalars().all())

    async def get(self, template_id: uuid.UUID, workspace_id: uuid.UUID) -> PptxTemplate | None:
        tpl = await self._db.get(PptxTemplate, template_id)
        return tpl if tpl is not None and tpl.workspace_id == workspace_id else None

    async def find_by_name(self, workspace_id: uuid.UUID, name: str) -> PptxTemplate | None:
        """Fuzzy pick for the chat path ("use the Omantel template")."""
        rows = await self.list_for_workspace(workspace_id)
        n = name.strip().lower()
        exact = next((t for t in rows if t.name.lower() == n), None)
        if exact is not None:
            return exact
        return next((t for t in rows if n in t.name.lower() or t.name.lower() in n), None)

    async def delete(self, template_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        tpl = await self.get(template_id, workspace_id)
        if tpl is None:
            return False
        await self._db.delete(tpl)
        await self._db.flush()
        return True
