"""The header bell: the caller's own notifications (Phase 18).

Every route acts on the signed-in user's rows only (and RLS repeats it).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.auth.base import UserResult
from sila.api.deps import get_current_user
from sila.core.utils.timestamps import utc_iso
from sila.db.models import Notification
from sila.db.repositories.notifications import NotificationRepository
from sila.db.session import get_db

router = APIRouter()

User = Annotated[UserResult, Depends(get_current_user)]
Db = Annotated[AsyncSession, Depends(get_db)]


def _payload(n: Notification) -> dict[str, object]:
    return {
        "id": str(n.id),
        "kind": n.kind,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "read": n.read_at is not None,
        "created_at": utc_iso(n.created_at),
    }


@router.get("/notifications")
async def list_notifications(
    user: User,
    db: Db,
    limit: int = Query(20, ge=1, le=50),  # noqa: B008
) -> dict[str, object]:
    repo = NotificationRepository(db)
    return {
        "items": [_payload(n) for n in await repo.latest(user.id, limit=limit)],
        "unread": await repo.unread_count(user.id),
    }


@router.post("/notifications/read-all")
async def read_all(user: User, db: Db) -> dict[str, int]:
    return {"marked": await NotificationRepository(db).mark_all_read(user.id)}


@router.post("/notifications/{notification_id}/read", status_code=204)
async def read_one(notification_id: uuid.UUID, user: User, db: Db) -> Response:
    """Idempotent. Only the caller's own row can change; an unknown or someone
    else's id is answered the same way, so ids reveal nothing."""
    await NotificationRepository(db).mark_read(user.id, notification_id)
    return Response(status_code=204)
