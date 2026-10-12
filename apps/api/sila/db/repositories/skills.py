"""Skills: the Bazaar's published open-source skills and a workspace's imports.

Complexity: every read is an indexed lookup — the listing uses
ix_skills_listing (published, category, installs DESC) and, with a query, the
GIN full-text index; a page is O(log n + page size).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import Skill


@dataclass(frozen=True)
class SkillFilters:
    query: str = ""
    category: str | None = None
    instructions_only: bool = False  # hide skills that ship scripts
    licensed_only: bool = False  # hide skills that state no licence
    sort: str = "popular"  # "popular" | "newest"


class SkillRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, skill_id: uuid.UUID) -> Skill | None:
        return await self._db.get(Skill, skill_id)

    async def get_by_upstream(self, source: str, skill_key: str) -> Skill | None:
        return (
            await self._db.execute(
                select(Skill).where(Skill.source == source, Skill.skill_key == skill_key)
            )
        ).scalar_one_or_none()

    async def imported_keys(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], Skill]:
        """Which of these upstream skills are already imported (one query)."""
        if not keys:
            return {}
        upstream = tuple_(Skill.source, Skill.skill_key)
        rows = (await self._db.execute(select(Skill).where(upstream.in_(keys)))).scalars()
        return {(row.source, row.skill_key): row for row in rows}

    async def imports_since(self, user_id: uuid.UUID, since: datetime) -> int:
        return (
            await self._db.execute(
                select(func.count())
                .select_from(Skill)
                .where(Skill.imported_by == user_id, Skill.imported_at >= since)
            )
        ).scalar_one()

    async def published_by_ids(self, ids: list[uuid.UUID]) -> list[Skill]:
        """The published skills among `ids`, in the order given."""
        if not ids:
            return []
        rows = (
            await self._db.execute(select(Skill).where(Skill.id.in_(ids), Skill.published))
        ).scalars()
        found = {row.id: row for row in rows}
        return [found[i] for i in ids if i in found]

    async def list_published(
        self, filters: SkillFilters, *, page: int, size: int
    ) -> tuple[list[Skill], int]:
        stmt = self._filtered(select(Skill, func.count().over()).where(Skill.published), filters)
        rows = (await self._db.execute(stmt.offset((page - 1) * size).limit(size))).all()
        return [row[0] for row in rows], (rows[0][1] if rows else 0)

    async def list_workspace(self, workspace_id: uuid.UUID, *, page: int, size: int) -> list[Skill]:
        """A workspace's own imports, newest first (drafts and rejections too)."""
        stmt = (
            select(Skill)
            .where(Skill.workspace_id == workspace_id)
            .order_by(Skill.imported_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        return list((await self._db.execute(stmt)).scalars())

    async def category_counts(self) -> dict[str, int]:
        """Published skills per category (for the filter chips)."""
        rows = await self._db.execute(
            select(Skill.category, func.count()).where(Skill.published).group_by(Skill.category)
        )
        return {category: count for category, count in rows.all()}

    def add(self, skill: Skill) -> None:
        self._db.add(skill)

    async def delete(self, skill: Skill) -> None:
        await self._db.delete(skill)
        await self._db.flush()

    @staticmethod
    def _filtered(stmt: Select, filters: SkillFilters) -> Select:  # type: ignore[type-arg]
        if filters.category:
            stmt = stmt.where(Skill.category == filters.category)
        if filters.instructions_only:
            stmt = stmt.where(Skill.has_scripts.is_(False))
        if filters.licensed_only:
            stmt = stmt.where(Skill.license.is_not(None))
        if filters.query.strip():
            match = text("skills.search @@ websearch_to_tsquery('english', :q)").bindparams(
                q=filters.query.strip()[:200]
            )
            stmt = stmt.where(match)
        if filters.sort == "newest":
            return stmt.order_by(Skill.imported_at.desc(), Skill.id)
        return stmt.order_by(Skill.installs.desc(), Skill.id)
