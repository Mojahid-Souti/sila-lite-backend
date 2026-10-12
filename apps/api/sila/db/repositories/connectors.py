"""Connector-binding repository (WS-9).

CRUD for `connector_bindings` — the per-user references to Nango connections.
No secret ever lives here; only the opaque `nango_connection_id` + resource
choices. RLS (per-user) is enforced in Postgres; the API runs as service_role,
so every query is also explicitly scoped by `user_id` here as defense in depth.

Complexity: each method is O(log n) via ix_connector_bindings_user_provider.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import ConnectorBinding


class ConnectorBindingRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(
        self, *, user_id: uuid.UUID, workspace_id: uuid.UUID, provider: str
    ) -> ConnectorBinding | None:
        result = await self._db.execute(
            select(ConnectorBinding).where(
                ConnectorBinding.user_id == user_id,
                ConnectorBinding.workspace_id == workspace_id,
                ConnectorBinding.provider == provider,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_user(
        self, *, user_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> list[ConnectorBinding]:
        result = await self._db.execute(
            select(ConnectorBinding).where(
                ConnectorBinding.user_id == user_id,
                ConnectorBinding.workspace_id == workspace_id,
            )
        )
        return list(result.scalars().all())

    async def upsert(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        provider: str,
        nango_connection_id: str,
        selected_resources: dict[str, Any] | None = None,
    ) -> ConnectorBinding:
        """Create or update the (user, workspace, provider) binding. Idempotent."""
        binding = await self.get(user_id=user_id, workspace_id=workspace_id, provider=provider)
        if binding is None:
            binding = ConnectorBinding(
                user_id=user_id,
                workspace_id=workspace_id,
                provider=provider,
                nango_connection_id=nango_connection_id,
                selected_resources=selected_resources or {},
            )
            self._db.add(binding)
        else:
            binding.nango_connection_id = nango_connection_id
            if selected_resources is not None:
                binding.selected_resources = selected_resources
        # get_db wraps the request in one transaction (begin()) and commits at the
        # end — repos must flush (persist + get generated ids), never commit, or they
        # close that transaction mid-request ("operate on closed transaction").
        await self._db.flush()
        await self._db.refresh(binding)
        return binding

    async def set_resources(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        provider: str,
        selected_resources: dict[str, Any],
    ) -> ConnectorBinding | None:
        binding = await self.get(user_id=user_id, workspace_id=workspace_id, provider=provider)
        if binding is None:
            return None
        binding.selected_resources = selected_resources
        # get_db wraps the request in one transaction (begin()) and commits at the
        # end — repos must flush (persist + get generated ids), never commit, or they
        # close that transaction mid-request ("operate on closed transaction").
        await self._db.flush()
        await self._db.refresh(binding)
        return binding

    async def delete(self, *, user_id: uuid.UUID, workspace_id: uuid.UUID, provider: str) -> bool:
        """Delete the binding row. Returns True if a row was removed."""
        result = await self._db.execute(
            delete(ConnectorBinding).where(
                ConnectorBinding.user_id == user_id,
                ConnectorBinding.workspace_id == workspace_id,
                ConnectorBinding.provider == provider,
            )
        )
        await self._db.flush()  # request-scoped transaction commits in get_db
        return bool(result.rowcount)  # type: ignore[attr-defined]
