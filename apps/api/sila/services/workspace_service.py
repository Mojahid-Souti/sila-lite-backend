import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from sila.core.constants import Action, Outcome, ResourceType, Role
from sila.db.models import Membership, Workspace
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.membership import MembershipRepository
from sila.db.repositories.organization import OrganizationRepository
from sila.db.repositories.workspace import WorkspaceRepository


def _slugify(name: str) -> str:
    """Convert name to lowercase-hyphenated slug. E.g. 'My Workspace' → 'my-workspace'."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")[:80]


class WorkspaceService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._workspaces = WorkspaceRepository(db)
        self._memberships = MembershipRepository(db)
        self._orgs = OrganizationRepository(db)
        self._audit = AuditRepository(db)

    async def create(self, name: str, owner_id: uuid.UUID, description: str = "") -> Workspace:
        """Create workspace under the owner's org (created on first use) and make the
        owner an Admin member. O(1) writes."""
        if await self._workspaces.name_exists_for_user(owner_id, name):
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"You already have a workspace named '{name.strip()}'. Choose a different name."
                ),
            )

        slug = await self._unique_slug(_slugify(name))

        # Attach to the owner's organization (Bazaar provenance), creating it once.
        org = await self._orgs.get_for_owner(owner_id)
        org_created = org is None
        if org is None:
            org = await self._orgs.create_for_owner(owner_id, name=await self._org_name(owner_id))

        async with self._db.begin_nested():
            ws = await self._workspaces.create(
                name=name,
                slug=slug,
                owner_id=owner_id,
                organization_id=org.id,
                description=description,
            )
            await self._memberships.create(workspace_id=ws.id, user_id=owner_id, role=Role.ADMIN)
            if org_created:
                await self._audit.record(
                    action=Action.ORGANIZATION_CREATE,
                    outcome=Outcome.SUCCESS,
                    actor_user_id=owner_id,
                    actor_workspace_id=ws.id,
                    resource_type=ResourceType.ORGANIZATION,
                    resource_id=org.id,
                )
            await self._audit.record(
                action=Action.WORKSPACE_UPDATE,
                outcome=Outcome.SUCCESS,
                actor_user_id=owner_id,
                actor_workspace_id=ws.id,
                resource_type=ResourceType.WORKSPACE,
                resource_id=ws.id,
            )

        return ws

    async def _org_name(self, owner_id: uuid.UUID) -> str:
        """A friendly default name for a new owner's org, from their profile
        (read straight from auth.users — reliable, unlike the GoTrue admin API)."""
        names = await self._orgs.display_names({owner_id})
        return f"{names.get(owner_id, 'My')} Organization"

    async def update(
        self, workspace_id: uuid.UUID, actor_id: uuid.UUID, name: str, description: str
    ) -> Workspace:
        """Update name and description. Re-slugifies on name change. O(1)."""
        from fastapi import HTTPException, status

        ws = await self._workspaces.get(workspace_id)
        if not ws:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")

        name_changed = ws.name.lower().strip() != name.lower().strip()
        if name_changed and await self._workspaces.name_exists_for_user(
            actor_id, name, exclude_id=workspace_id
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"You already have a workspace named '{name.strip()}'. Choose a different name."
                ),
            )

        new_slug = ws.slug if not name_changed else await self._unique_slug(_slugify(name))

        async with self._db.begin_nested():
            ws = await self._workspaces.update(
                ws, name=name, slug=new_slug, description=description
            )
            await self._audit.record(
                action=Action.WORKSPACE_UPDATE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.WORKSPACE,
                resource_id=workspace_id,
            )
        return ws

    async def delete(self, workspace_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        """Delete workspace; DB CASCADE removes memberships, agents, KBs. O(1)."""
        from fastapi import HTTPException, status

        ws = await self._workspaces.get(workspace_id)
        if not ws:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
        if ws.owner_id != actor_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the workspace owner can delete it",
            )

        async with self._db.begin_nested():
            await self._audit.record(
                action=Action.WORKSPACE_DELETE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.WORKSPACE,
                resource_id=workspace_id,
            )
            await self._workspaces.delete(ws)

    async def invite(
        self,
        workspace_id: uuid.UUID,
        inviter_id: uuid.UUID,
        invitee_id: uuid.UUID,
        role: Role,
    ) -> Membership:
        async with self._db.begin_nested():
            m = await self._memberships.create(
                workspace_id=workspace_id,
                user_id=invitee_id,
                role=role,
                invited_by=inviter_id,
            )
            await self._audit.record(
                action=Action.WORKSPACE_INVITE,
                outcome=Outcome.SUCCESS,
                actor_user_id=inviter_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.MEMBERSHIP,
                resource_id=m.id,
            )
        return m

    async def update_role(
        self,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        target_user_id: uuid.UUID,
        new_role: Role,
    ) -> Membership | None:
        async with self._db.begin_nested():
            m = await self._memberships.update_role(workspace_id, target_user_id, new_role)
            if m:
                await self._audit.record(
                    action=Action.MEMBERSHIP_UPDATE,
                    outcome=Outcome.SUCCESS,
                    actor_user_id=actor_id,
                    actor_workspace_id=workspace_id,
                    resource_type=ResourceType.MEMBERSHIP,
                    resource_id=m.id,
                )
        return m

    async def remove_member(
        self, workspace_id: uuid.UUID, actor_id: uuid.UUID, target_user_id: uuid.UUID
    ) -> None:
        async with self._db.begin_nested():
            await self._memberships.remove(workspace_id, target_user_id)
            await self._audit.record(
                action=Action.MEMBERSHIP_UPDATE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.MEMBERSHIP,
            )

    async def _unique_slug(self, base: str) -> str:
        """Append numeric suffix if slug is taken. Max 5 attempts."""
        slug = base
        for i in range(1, 6):
            if not await self._workspaces.slug_exists(slug):
                return slug
            slug = f"{base}-{i}"
        return f"{base}-{uuid.uuid4().hex[:6]}"
