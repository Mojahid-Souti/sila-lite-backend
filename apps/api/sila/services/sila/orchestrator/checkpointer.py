"""Postgres checkpointer for the SILA graph — durable, time-travel state.

A single lazily-initialised AsyncPostgresSaver over a psycopg-v3 connection pool
on the same Supabase Postgres SQLAlchemy uses (different driver/pool). Lazy so it
never spins up while SILA_ORCHESTRATOR=loop (the default).

Complexity: pool init + `setup()` once (idempotent DDL); O(1) per checkpoint.
"""

from __future__ import annotations

import asyncio

import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from sila.core.config import settings

log = structlog.get_logger("sila.orchestrator")

_lock = asyncio.Lock()
_pool: AsyncConnectionPool | None = None
_saver: AsyncPostgresSaver | None = None


async def get_checkpointer() -> AsyncPostgresSaver:
    """Return the shared checkpointer, opening the pool + running setup() once."""
    global _pool, _saver
    if _saver is not None:
        return _saver
    async with _lock:
        if _saver is not None:  # re-check after acquiring the lock
            return _saver
        pool = AsyncConnectionPool(
            conninfo=settings.database_url_psycopg,
            max_size=10,
            open=False,
            # The checkpointer needs autocommit + dict rows; prepare_threshold=0
            # avoids prepared-statement clashes through poolers.
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open()
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await saver.setup()  # idempotent: creates the checkpoint tables
        _pool, _saver = pool, saver
        log.info("sila.checkpointer.ready")
        return saver


async def close_checkpointer() -> None:
    """Close the pool on shutdown (best-effort)."""
    global _pool, _saver
    if _pool is not None:
        await _pool.close()
    _pool, _saver = None, None
