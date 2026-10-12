"""What a new SILA session announces and carries: the template, the skills.

Called once per WebSocket hello. The browser sends a template ID and skill IDs
only; names and instructions come from the server's own registry and the
published `skills` rows. A resumed session (the Recents list, a reconnect)
keeps the skills it had; only a skill new to the session is announced.

Complexity: O(k) for k attached skills (k <= settings.sila_max_skills_per_session).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from sila.core.config import settings
from sila.core.constants import Action, Outcome, ResourceType
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.sila import SilaRepository
from sila.services.bazaar_templates import name_for
from sila.services.notification_service import NotificationKind, notify
from sila.services.skills.context import AttachedSkill, load_attached, skills_block

_SKILLS_KEY = "attached_skills"


async def attach_skills(
    db: AsyncSession,
    *,
    requested: object,
    session_id: uuid.UUID,
    resumed: bool,
    confidential: bool,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> list[AttachedSkill]:
    """The session's skills, saved on it and announced when new.

    `requested` is the hello's `skills`: None (absent) keeps what a resumed
    session had; a list — even empty — is the user's choice now. A confidential
    (OKR) session carries none: its turns are run by code, and third-party
    instructions have no place beside a confidential sheet.
    """
    if confidential:
        return []
    repo = SilaRepository(db)
    before = (await repo.get_session_metadata(session_id)).get(_SKILLS_KEY) if resumed else None
    if requested is None:
        requested = before
    skills = await load_attached(db, requested) if requested else []
    if resumed or skills:
        await repo.merge_session_metadata(session_id, {_SKILLS_KEY: [str(s.id) for s in skills]})
    if not skills:
        await db.commit()
        return []
    # Announce only what is new to this session: a reconnect re-sends the same
    # skills; a skill added mid-conversation is new even in a resumed session.
    already = set(before) if isinstance(before, list) else set()
    fresh = [skill for skill in skills if str(skill.id) not in already]
    if fresh:
        audit = AuditRepository(db)
        for skill in fresh:
            await audit.record(
                action=Action.SKILL_ATTACH,
                outcome=Outcome.SUCCESS,
                actor_user_id=user_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.SKILL,
                resource_id=skill.id,
                metadata={"session": str(session_id)},
            )
            await notify(
                db,
                user_id=user_id,
                workspace_id=workspace_id,
                kind=NotificationKind.SKILL_ATTACHED,
                title=f"Skill attached: {skill.name}",
                body="It will guide this chat.",
                link=f"/bazaar?tab=skills&skill={skill.id}",
            )
    await db.commit()
    return skills


def skills_prompt(skills: list[AttachedSkill]) -> str:
    """The fenced prompt block for the session's skills ('' for none)."""
    return skills_block(skills, max_chars=settings.sila_skill_prompt_chars)


def skills_frame(skills: list[AttachedSkill]) -> dict[str, object]:
    """What the chat shows as chips: the attached skills' ids and names."""
    return {"type": "skills", "items": [{"id": str(s.id), "name": s.name} for s in skills]}


async def announce_template(
    db: AsyncSession,
    *,
    template_id: object,
    resumed: bool,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """A bell entry when a new chat starts on a template."""
    name = name_for(str(template_id).strip()) if template_id else ""
    if resumed or not name:
        return
    await notify(
        db,
        user_id=user_id,
        workspace_id=workspace_id,
        kind=NotificationKind.TEMPLATE_SELECTED,
        title=f"Template in use: {name}",
        body="Your chat started on this template.",
        link="/bazaar",
    )
    await db.commit()
