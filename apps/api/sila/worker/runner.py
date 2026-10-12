"""KB document-processing worker loop.

Design (Postgres-backed durable queue):
  - The API INSERTs a kb_jobs row (status='queued') and ``NOTIFY kb_jobs`` in the
    upload transaction.
  - This worker LISTENs on the 'kb_jobs' channel for instant wake-up, and falls
    back to a periodic poll so a missed NOTIFY (or a job left by a crashed worker)
    is still picked up.
  - Each cycle: drain all claimable jobs (FOR UPDATE SKIP LOCKED, one at a time to
    respect the single-GPU embedding budget), then reclaim any stale 'processing'
    rows, then wait for the next NOTIFY or timeout.

Concurrency: claiming with SKIP LOCKED means running N worker replicas is safe —
no two ever grab the same row. We run one replica in the POC (12 GB VRAM budget).
"""

import asyncio
import contextlib
import logging

import asyncpg  # type: ignore[import-untyped]

from sila.core.config import settings
from sila.db.repositories.knowledge_base import (
    KB_JOBS_CHANNEL,
    DocumentRepository,
    KBJobRepository,
)
from sila.db.session import AsyncSessionLocal
from sila.services.kb_service import build_kb_service

logger = logging.getLogger("sila.worker")

# Safety-net poll cadence (a NOTIFY normally wakes us sooner).
POLL_INTERVAL_SECONDS = 10.0
# A 'processing' row older than this is assumed orphaned by a crashed worker.
STALE_RECLAIM_SECONDS = 600


async def _process_one() -> bool:
    """Claim and process a single job. Returns False when the queue is empty."""
    # Claim in a short transaction so the row flips to 'processing' immediately.
    async with AsyncSessionLocal() as session:
        jobs = KBJobRepository(session)
        claimed = await jobs.claim_next()
        await session.commit()

    if claimed is None:
        return False

    logger.info(
        "claimed kb_job %s (doc=%s, attempt %d)",
        claimed.id,
        claimed.document_id,
        claimed.attempts,
    )

    # Process in its own transaction — long-running, so never hold the claim txn open.
    async with AsyncSessionLocal() as session:
        jobs = KBJobRepository(session)
        service = build_kb_service(session)
        try:
            await service.process_document(
                doc_id=claimed.document_id,
                kb_id=claimed.kb_id,
                workspace_id=claimed.workspace_id,
            )
            await jobs.mark_done(claimed.id)
            await session.commit()
            logger.info("kb_job %s done (doc=%s)", claimed.id, claimed.document_id)
        except Exception as exc:  # noqa: BLE001 — log + park/retry, never crash the loop
            logger.error(
                "kb_job %s failed (doc=%s): %s", claimed.id, claimed.document_id, exc, exc_info=True
            )
            await session.rollback()
            await DocumentRepository(session).set_status(
                claimed.document_id, "failed", error_detail=str(exc)[:500]
            )
            will_retry = await jobs.mark_failed(claimed.id, str(exc), attempts=claimed.attempts)
            await session.commit()
            if will_retry:
                logger.info("kb_job %s requeued for retry", claimed.id)
    return True


async def _reclaim_stale() -> None:
    async with AsyncSessionLocal() as session:
        reclaimed = await KBJobRepository(session).reclaim_stale(
            older_than_seconds=STALE_RECLAIM_SECONDS
        )
        await session.commit()
    if reclaimed:
        logger.warning("reclaimed %d stale processing job(s)", reclaimed)


async def _open_listener(wake: asyncio.Event) -> asyncpg.Connection:
    """Dedicated raw asyncpg connection LISTENing on the kb_jobs channel."""
    dsn = str(settings.database_url).replace("+asyncpg", "")
    conn = await asyncpg.connect(dsn)
    await conn.add_listener(KB_JOBS_CHANNEL, lambda *_: wake.set())
    return conn


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info(
        "KB worker starting — LISTEN %s, poll every %ss", KB_JOBS_CHANNEL, POLL_INTERVAL_SECONDS
    )

    wake = asyncio.Event()
    listener = await _open_listener(wake)
    try:
        while True:
            # Drain everything claimable right now.
            while await _process_one():
                pass
            await _reclaim_stale()

            # Sleep until a NOTIFY arrives or the poll timeout elapses.
            wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=POLL_INTERVAL_SECONDS)
    finally:
        await listener.close()
