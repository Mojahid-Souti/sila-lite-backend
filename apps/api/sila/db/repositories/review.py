"""ReviewRepository — agent ratings and reviews.

Indexes used:
  - ix_reviews_agent_created_at for list  (O(log n))
  - uq_review unique (agent_id, author_id) for upsert/exists  (O(1))
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import Review

# Minimum number of reviews required to reveal avg rating (k-anonymity)
_K_ANON_THRESHOLD = 3


class ReviewRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def upsert(
        self,
        *,
        agent_id: uuid.UUID,
        workspace_id: uuid.UUID,
        author_id: uuid.UUID,
        rating: int,
        body: str,
    ) -> Review:
        """Create or replace a user's review for an agent. O(1) via unique index."""
        existing = await self._get_by_author(agent_id=agent_id, author_id=author_id)
        if existing:
            existing.rating = rating
            existing.body = body
            await self._db.flush()
            return existing
        review = Review(
            agent_id=agent_id,
            workspace_id=workspace_id,
            author_id=author_id,
            rating=rating,
            body=body,
        )
        self._db.add(review)
        await self._db.flush()
        return review

    async def _get_by_author(self, *, agent_id: uuid.UUID, author_id: uuid.UUID) -> Review | None:
        result = await self._db.execute(
            select(Review).where(Review.agent_id == agent_id, Review.author_id == author_id)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        agent_id: uuid.UUID,
        *,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[Review], int]:
        """List reviews for an agent, newest first. O(log n)."""
        base = select(Review).where(Review.agent_id == agent_id)
        total_result = await self._db.execute(select(func.count()).select_from(base.subquery()))
        total = total_result.scalar_one()
        rows = await self._db.execute(
            base.order_by(Review.created_at.desc()).offset((page - 1) * size).limit(size)
        )
        return list(rows.scalars().all()), total

    async def avg_rating(self, agent_id: uuid.UUID) -> dict[str, object]:
        """Return {avg, count} for an agent.

        avg is None when count < _K_ANON_THRESHOLD (k-anonymity).
        O(log k) via ix_reviews_agent_created_at covering index.
        """
        result = await self._db.execute(
            select(func.avg(Review.rating), func.count(Review.id)).where(
                Review.agent_id == agent_id
            )
        )
        avg_raw, count = result.one()
        avg: float | None = None
        if count >= _K_ANON_THRESHOLD and avg_raw is not None:
            avg = round(float(Decimal(str(avg_raw))), 1)
        return {"avg": avg, "count": count}
