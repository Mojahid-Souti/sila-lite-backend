import secrets
import uuid

from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import AsyncSession

from sila.core.constants import Action, Outcome, ResourceType
from sila.db.models import ApiKey
from sila.db.repositories.audit import AuditRepository

_ph = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)

# Key format: wk_<random 40 hex chars>
_PREFIX = "wk_"
_KEY_BYTES = 20  # 40 hex chars


class ApiKeyService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._audit = AuditRepository(db)

    async def generate(
        self, workspace_id: uuid.UUID, name: str, created_by: uuid.UUID
    ) -> tuple[ApiKey, str]:
        """
        Returns (ApiKey record, plaintext key).
        The plaintext key is shown to the user ONCE and never stored.
        O(1) — single DB insert.
        """
        raw = _PREFIX + secrets.token_hex(_KEY_BYTES)
        key_hash = _ph.hash(raw)
        key_prefix = raw[: len(_PREFIX) + 8]  # "wk_" + first 8 hex chars

        async with self._db.begin_nested():
            record = ApiKey(
                workspace_id=workspace_id,
                name=name,
                key_hash=key_hash,
                key_prefix=key_prefix,
                created_by=created_by,
            )
            self._db.add(record)
            await self._db.flush()

            await self._audit.record(
                action=Action.API_KEY_MANAGE,
                outcome=Outcome.SUCCESS,
                actor_user_id=created_by,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.API_KEY,
                resource_id=record.id,
            )

        return record, raw

    async def revoke(
        self, key_id: uuid.UUID, actor_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> ApiKey | None:
        from datetime import UTC, datetime

        record = await self._db.get(ApiKey, key_id)
        if not record or record.workspace_id != workspace_id:
            return None

        async with self._db.begin_nested():
            record.revoked_at = datetime.now(UTC)
            await self._db.flush()
            await self._audit.record(
                action=Action.API_KEY_MANAGE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.API_KEY,
                resource_id=key_id,
                metadata={"action": "revoke"},
            )

        return record
