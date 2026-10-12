"""Organization repository — the tenancy layer above workspaces.

An organization groups a person's workspaces; its name is the public provenance
label the Bazaar shows ("Created by my Org" vs "Third-party"). One org per owner
(the workspace owner), so `get_or_create_for_owner` is the single write path.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import Membership, Organization, Workspace


class OrganizationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, org_id: uuid.UUID) -> Organization | None:
        return await self._db.get(Organization, org_id)

    async def get_for_owner(self, owner_id: uuid.UUID) -> Organization | None:
        """The owner's org, or None. O(1) via the unique (owner_id) constraint."""
        return (
            await self._db.execute(select(Organization).where(Organization.owner_id == owner_id))
        ).scalar_one_or_none()

    async def create_for_owner(self, owner_id: uuid.UUID, *, name: str) -> Organization:
        """Create the owner's org (call only after get_for_owner returned None)."""
        slug = f"org-{re.sub(r'[^a-z0-9]', '', str(owner_id).lower())[:12]}"
        org = Organization(name=name.strip()[:120] or "Organization", slug=slug, owner_id=owner_id)
        self._db.add(org)
        await self._db.flush()
        return org

    async def org_ids_for_user(self, user_id: uuid.UUID) -> set[uuid.UUID]:
        """The organizations the user belongs to — the orgs of every workspace they
        are a member of. Drives the 'My Org' Bazaar bucket. O(memberships)."""
        rows = await self._db.execute(
            select(Workspace.organization_id)
            .join(Membership, Membership.workspace_id == Workspace.id)
            .where(Membership.user_id == user_id)
        )
        return {r[0] for r in rows if r[0] is not None}

    async def names_by_ids(self, org_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Batch org id → name for provenance labels. O(n)."""
        if not org_ids:
            return {}
        rows = await self._db.execute(
            select(Organization.id, Organization.name).where(Organization.id.in_(org_ids))
        )
        return {r.id: r.name for r in rows}

    async def org_id_by_workspace(
        self, workspace_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Batch workspace id → its organization_id. O(n)."""
        if not workspace_ids:
            return {}
        rows = await self._db.execute(
            select(Workspace.id, Workspace.organization_id).where(Workspace.id.in_(workspace_ids))
        )
        return {r.id: r.organization_id for r in rows}

    async def display_names(self, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Batch user id → display name (full_name, else the email local-part) read
        straight from auth.users — the reliable source (the GoTrue admin HTTP API is
        flaky here). Used for Bazaar creator labels + org naming. O(n)."""
        ids = {u for u in user_ids if u is not None}
        if not ids:
            return {}
        rows = await self._db.execute(
            text(
                "SELECT id::text AS id, "
                "COALESCE(NULLIF(raw_user_meta_data->>'full_name', ''), "
                "split_part(email, '@', 1)) AS name "
                "FROM auth.users WHERE id::text IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": [str(u) for u in ids]},
        )
        return {uuid.UUID(r.id): (r.name or "Unknown") for r in rows}
