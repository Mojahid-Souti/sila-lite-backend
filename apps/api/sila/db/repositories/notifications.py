"""A user's notifications (the header bell). Every query is scoped to the owner.

Complexity: the list is O(log n + limit) on ix_notifications_user_created; the
unread count O(log n + unread) on the partial ix_notifications_unread.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import Notification


def _now() -> datetime:
    # Naive UTC, like the column (a naive/aware mix breaks asyncpg comparisons).
    return datetime.now(UTC).replace(tzinfo=None)


class NotificationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    def add(self, notification: Notification) -> None:
        self._db.add(notification)

    async def latest(self, user_id: uuid.UUID, *, limit: int) -> list[Notification]:
        stmt = (
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
        )
        return list((await self._db.execute(stmt)).scalars())

    async def unread_count(self, user_id: uuid.UUID) -> int:
        return (
            await self._db.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            )
        ).scalar_one()

    async def mark_read(self, user_id: uuid.UUID, notification_id: uuid.UUID) -> bool:
        """True when the user's own unread notification was marked; idempotent."""
        result = await self._db.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=_now())
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        result = await self._db.execute(
            update(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            .values(read_at=_now())
        )
        return int(result.rowcount)  # type: ignore[attr-defined]
