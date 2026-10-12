import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.core.constants import Role
from sila.db.models import Membership


class MembershipRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        role: Role,
        invited_by: uuid.UUID | None = None,
    ) -> Membership:
        m = Membership(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            invited_by=invited_by,
        )
        self._db.add(m)
        await self._db.flush()
        return m

    async def get(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> Membership | None:
        """O(1) via unique index on (workspace_id, user_id)."""
        result = await self._db.execute(
            select(Membership).where(
                Membership.workspace_id == workspace_id,
                Membership.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_workspace(self, workspace_id: uuid.UUID) -> list[Membership]:
        result = await self._db.execute(
            select(Membership)
            .where(Membership.workspace_id == workspace_id)
            .order_by(Membership.created_at)
        )
        return list(result.scalars().all())

    async def update_role(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID, role: Role
    ) -> Membership | None:
        m = await self.get(workspace_id, user_id)
        if m:
            m.role = role
            await self._db.flush()
        return m

    async def remove(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self._db.execute(
            delete(Membership).where(
                Membership.workspace_id == workspace_id,
                Membership.user_id == user_id,
            )
        )
