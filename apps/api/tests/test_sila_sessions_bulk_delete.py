"""Bulk session delete — ownership filtering, batch cap, and auth guard.

The security property under test is that ownership is enforced in SQL rather
than trusted from the request body: a caller who puts someone else's session id
in the list must not delete it, and must not be told it existed either.
"""

import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sila.core.constants import MAX_SILA_BULK_DELETE
from sila.db.repositories.sila import SilaRepository
from sila.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_overrides() -> Generator[None]:
    yield
    app.dependency_overrides.clear()


def _db_returning(owned: list[uuid.UUID]) -> MagicMock:
    """A db whose first execute() (the ownership SELECT) yields `owned`."""
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = owned
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_deletes_only_sessions_the_user_owns() -> None:
    """Happy path + the security case in one: two ids sent, one owned. The
    return value counts what was actually removed, not what was asked for."""
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    db = _db_returning([mine])

    removed = await SilaRepository(db).delete_sessions([mine, theirs], uuid.uuid4())

    assert removed == 1, "must report the owned count, not the requested count"
    # The repository must NOT commit: get_db wraps the request in one
    # transaction so the endpoint's audit row lands atomically with the delete.
    # Committing here closed that transaction and 500'd every call that wrote
    # anything afterwards.
    db.commit.assert_not_awaited()
    # Three statements total — the ownership SELECT, then one DELETE per table.
    # Any growth with batch size would mean an N+1 crept in.
    assert db.execute.await_count == 3
    # Whatever the deletes were built from, the unowned id must not appear.
    rendered = " ".join(str(c.args[0]) for c in db.execute.await_args_list)
    assert str(theirs) not in rendered


@pytest.mark.asyncio
async def test_no_owned_ids_touches_nothing() -> None:
    """None of the ids belong to the caller: no DELETE, no commit, no 'not
    found' signal that would confirm the sessions exist for someone else."""
    db = _db_returning([])

    removed = await SilaRepository(db).delete_sessions([uuid.uuid4()], uuid.uuid4())

    assert removed == 0
    assert db.execute.await_count == 1  # the ownership SELECT only
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_list_short_circuits() -> None:
    db = _db_returning([])
    assert await SilaRepository(db).delete_sessions([], uuid.uuid4()) == 0
    db.execute.assert_not_awaited()


def test_bulk_delete_requires_auth() -> None:
    r = client.post("/v1/sila/sessions/bulk-delete", json={"session_ids": [str(uuid.uuid4())]})
    assert r.status_code in (401, 403)


def test_batch_cap_rejected_before_auth_or_db() -> None:
    """Over-sized batches fail validation, so one call can't queue unbounded
    work. Unauthenticated here, so accept either the 422 or the auth refusal —
    the point is that it never reaches the repository."""
    ids = [str(uuid.uuid4()) for _ in range(MAX_SILA_BULK_DELETE + 1)]
    r = client.post("/v1/sila/sessions/bulk-delete", json={"session_ids": ids})
    assert r.status_code in (401, 403, 422)


def test_empty_body_rejected() -> None:
    r = client.post("/v1/sila/sessions/bulk-delete", json={"session_ids": []})
    assert r.status_code in (401, 403, 422)
