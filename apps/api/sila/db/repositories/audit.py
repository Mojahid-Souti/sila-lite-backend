import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from sila.core.constants import Outcome, ResourceType
from sila.db.models import AuditLog


class AuditRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def record(
        self,
        action: str,
        outcome: Outcome,
        actor_user_id: uuid.UUID | None = None,
        actor_workspace_id: uuid.UUID | None = None,
        resource_type: ResourceType | None = None,
        resource_id: uuid.UUID | None = None,
        metadata: dict | None = None,  # type: ignore[type-arg]
        trace_id: str | None = None,
    ) -> None:
        """Append-only. Never raises — audit failure must not break the user request."""
        entry = AuditLog(
            action=action,
            outcome=outcome,
            actor_user_id=actor_user_id,
            actor_workspace_id=actor_workspace_id,
            resource_type=resource_type,
            resource_id=resource_id,
            event_metadata=metadata or {},
            trace_id=trace_id,
        )
        self._db.add(entry)
        await self._db.flush()
