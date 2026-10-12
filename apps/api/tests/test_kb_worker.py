"""Unit tests for the KB document-processing worker + queue repository.

DB-backed behaviour (FOR UPDATE SKIP LOCKED concurrency, LISTEN/NOTIFY) is in
the manual checklist; these cover the retry logic and the worker's claim →
process → done/fail orchestration with mocked sessions.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from pytest import MonkeyPatch

from sila.db.repositories.knowledge_base import (
    MAX_JOB_ATTEMPTS,
    ClaimedJob,
    KBJobRepository,
)
from sila.worker import runner


class _FakeSession:
    """Async-context-manager stand-in for AsyncSessionLocal()."""

    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _claim(attempts: int = 1) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        attempts=attempts,
    )


# ---------------------------------------------------------------------------
# KBJobRepository
# ---------------------------------------------------------------------------


async def test_mark_failed_requeues_until_max_attempts() -> None:
    """attempts < MAX -> requeue (True); at/over MAX -> park as failed (False)."""
    repo = KBJobRepository(AsyncMock())
    job = uuid.uuid4()
    assert await repo.mark_failed(job, "boom", attempts=1) is True
    assert await repo.mark_failed(job, "boom", attempts=MAX_JOB_ATTEMPTS - 1) is True
    assert await repo.mark_failed(job, "boom", attempts=MAX_JOB_ATTEMPTS) is False
    assert await repo.mark_failed(job, "boom", attempts=MAX_JOB_ATTEMPTS + 1) is False


async def test_enqueue_inserts_and_notifies() -> None:
    """Enqueue adds a row and issues NOTIFY so the worker wakes on commit."""
    session = AsyncMock()
    session.add = MagicMock()  # add() is synchronous
    repo = KBJobRepository(session)

    await repo.enqueue(document_id=uuid.uuid4(), kb_id=uuid.uuid4(), workspace_id=uuid.uuid4())

    session.add.assert_called_once()
    session.flush.assert_awaited_once()
    issued = [str(call.args[0]) for call in session.execute.await_args_list]
    assert any("NOTIFY" in sql for sql in issued)


# ---------------------------------------------------------------------------
# Worker orchestration (_process_one)
# ---------------------------------------------------------------------------


def _patch_common(monkeypatch: MonkeyPatch, jobs: MagicMock, service: MagicMock) -> None:
    monkeypatch.setattr(runner, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(runner, "KBJobRepository", lambda _session: jobs)
    monkeypatch.setattr(runner, "build_kb_service", lambda _session: service)


async def test_process_one_returns_false_when_queue_empty(monkeypatch: MonkeyPatch) -> None:
    jobs = MagicMock()
    jobs.claim_next = AsyncMock(return_value=None)
    service = MagicMock()
    service.process_document = AsyncMock()
    _patch_common(monkeypatch, jobs, service)

    assert await runner._process_one() is False
    service.process_document.assert_not_awaited()


async def test_process_one_success_marks_job_done(monkeypatch: MonkeyPatch) -> None:
    claimed = _claim()
    jobs = MagicMock()
    jobs.claim_next = AsyncMock(return_value=claimed)
    jobs.mark_done = AsyncMock()
    jobs.mark_failed = AsyncMock()
    service = MagicMock()
    service.process_document = AsyncMock()
    _patch_common(monkeypatch, jobs, service)

    assert await runner._process_one() is True
    service.process_document.assert_awaited_once_with(
        doc_id=claimed.document_id, kb_id=claimed.kb_id, workspace_id=claimed.workspace_id
    )
    jobs.mark_done.assert_awaited_once_with(claimed.id)
    jobs.mark_failed.assert_not_awaited()


async def test_process_one_failure_marks_doc_and_job_failed(monkeypatch: MonkeyPatch) -> None:
    claimed = _claim(attempts=1)
    jobs = MagicMock()
    jobs.claim_next = AsyncMock(return_value=claimed)
    jobs.mark_done = AsyncMock()
    jobs.mark_failed = AsyncMock(return_value=True)
    service = MagicMock()
    service.process_document = AsyncMock(side_effect=RuntimeError("parse exploded"))
    docs = MagicMock()
    docs.set_status = AsyncMock()
    _patch_common(monkeypatch, jobs, service)
    monkeypatch.setattr(runner, "DocumentRepository", lambda _session: docs)

    assert await runner._process_one() is True
    jobs.mark_done.assert_not_awaited()
    docs.set_status.assert_awaited_once()
    assert docs.set_status.await_args.args[1] == "failed"
    jobs.mark_failed.assert_awaited_once()
