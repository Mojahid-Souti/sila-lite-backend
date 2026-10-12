"""Sliding-window rate limit + daily quota, backed by `api_request_log`.

Two checks per request:
  - per-minute: COUNT(*) WHERE api_key_id=? AND ts > now() - interval '60s'
  - per-day:    COUNT(*) WHERE api_key_id=? AND ts > now() - interval '24h'

Each is O(log n) via the `(api_key_id, ts DESC)` index. We do them as one
combined query to halve the round-trips.

Complexity: O(log n + k) where n = rows in the index for this key, k = rows
returned (always <= rate limit + 1, well under 100 for any sane limit).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import ApiRequestLog


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a rate-limit check. `allowed=False` -> caller maps to 429."""

    allowed: bool
    requests_last_minute: int
    requests_today: int
    retry_after_seconds: int = 0
    limit_minute: int = 0
    limit_day: int = 0


class RateLimitService:
    """Sliding-window counter. One instance per request is cheap (stateless)."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        per_minute: int,
        per_day: int,
    ) -> None:
        self._db = db
        self._per_minute = per_minute
        self._per_day = per_day

    async def check(self, api_key_id: uuid.UUID) -> RateLimitResult:
        """Read both windowed counts in a single query."""
        now = datetime.utcnow()
        one_min_ago = now - timedelta(seconds=60)
        one_day_ago = now - timedelta(hours=24)

        # SUM-of-CASE pattern: one index scan, two windowed counts.
        result = await self._db.execute(
            select(
                func.count().filter(ApiRequestLog.ts > one_min_ago).label("last_minute"),
                func.count().filter(ApiRequestLog.ts > one_day_ago).label("last_day"),
            ).where(ApiRequestLog.api_key_id == api_key_id, ApiRequestLog.ts > one_day_ago)
        )
        row = result.one()
        last_minute = int(row.last_minute or 0)
        last_day = int(row.last_day or 0)

        if last_minute >= self._per_minute:
            return RateLimitResult(
                allowed=False,
                requests_last_minute=last_minute,
                requests_today=last_day,
                retry_after_seconds=60,
                limit_minute=self._per_minute,
                limit_day=self._per_day,
            )
        if last_day >= self._per_day:
            return RateLimitResult(
                allowed=False,
                requests_last_minute=last_minute,
                requests_today=last_day,
                retry_after_seconds=3600,
                limit_minute=self._per_minute,
                limit_day=self._per_day,
            )
        return RateLimitResult(
            allowed=True,
            requests_last_minute=last_minute,
            requests_today=last_day,
            limit_minute=self._per_minute,
            limit_day=self._per_day,
        )

    async def record(
        self,
        *,
        api_key_id: uuid.UUID,
        workspace_id: uuid.UUID,
        agent_id: uuid.UUID | None,
        endpoint: str,
        status_code: int,
        latency_ms: int,
    ) -> None:
        """Append-only. Caller wraps in their own transaction."""
        row = ApiRequestLog(
            api_key_id=api_key_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            endpoint=endpoint,
            status_code=status_code,
            latency_ms=latency_ms,
        )
        self._db.add(row)
        await self._db.flush()
