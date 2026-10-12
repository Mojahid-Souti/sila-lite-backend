"""Notifications for the header bell — what happened that a user should see.

Written server-side where the event happens (a hire, a template or skill
picked in the chat, a skill import vetted), so the bell is the same on every
device. Like the audit log, a notification must never break the request that
caused it: the row is added inside a savepoint, and a failure is logged and
dropped. Titles and bodies are built by code from names already shown to the
user — never from free text a third party wrote.

Complexity: O(1) per notification.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import Notification
from sila.services.skills.parse import clean_line

log = structlog.get_logger(__name__)

_TITLE_CHARS = 160
_BODY_CHARS = 500


class NotificationKind(StrEnum):
    AGENT_HIRED = "agent.hired"
    TEMPLATE_SELECTED = "template.selected"
    SKILL_ATTACHED = "skill.attached"
    SKILL_IMPORTED = "skill.imported"
    SKILL_PUBLISHED = "skill.published"


def _internal(link: str | None) -> str | None:
    """App-internal paths only ("//host" is protocol-relative, so it is not)."""
    return link if link and link.startswith("/") and not link.startswith("//") else None


async def notify(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    kind: NotificationKind,
    title: str,
    body: str = "",
    link: str | None = None,
    workspace_id: uuid.UUID | None = None,
) -> None:
    """Add one notification for `user_id`. Never raises."""
    try:
        async with db.begin_nested():
            db.add(
                Notification(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    kind=kind.value,
                    title=clean_line(title, _TITLE_CHARS),
                    body=clean_line(body, _BODY_CHARS),
                    link=_internal(link),
                )
            )
    except SQLAlchemyError:
        log.warning("notification.write_failed", kind=kind.value)
