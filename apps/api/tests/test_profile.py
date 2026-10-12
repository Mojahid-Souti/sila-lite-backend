"""PATCH /v1/auth/profile — a user renaming themselves.

The regression these pin: the write persisted to GoTrue but every read dropped
`full_name`, so the settings field stayed empty and the greeting kept falling
back to the email handle. The name has to survive the round-trip.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sila.adapters.auth.base import UserResult
from sila.api.deps import get_access_token, get_auth_service, get_current_user
from sila.db.session import get_db
from sila.main import app

client = TestClient(app)

_USER = UserResult(id=uuid.uuid4(), email="u@example.com", email_confirmed=True)


@pytest.fixture(autouse=True)
def clear_overrides() -> Generator[None]:
    yield
    app.dependency_overrides.clear()


def _wire(updated: UserResult) -> AsyncMock:
    """Override GoTrue, the caller identity, the raw token, and the DB session
    (so the audit write runs without a real database)."""
    svc = AsyncMock()
    svc.update_user_metadata.return_value = updated
    app.dependency_overrides[get_auth_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: _USER
    app.dependency_overrides[get_access_token] = lambda: "fake-token"

    async def _fake_db() -> AsyncGenerator[object]:
        db = MagicMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        yield db

    app.dependency_overrides[get_db] = _fake_db
    return svc


def _named(name: str | None) -> UserResult:
    return UserResult(id=_USER.id, email=_USER.email, email_confirmed=True, full_name=name)


def test_profile_update_returns_the_new_name() -> None:
    """Without full_name on the response model the save looked like a no-op."""
    svc = _wire(_named("Mojahid Al Souti"))
    r = client.patch("/v1/auth/profile", json={"full_name": "Mojahid Al Souti"})
    assert r.status_code == 200
    assert r.json()["full_name"] == "Mojahid Al Souti"
    svc.update_user_metadata.assert_awaited_once_with(
        "fake-token", {"full_name": "Mojahid Al Souti"}
    )


def test_me_returns_the_display_name() -> None:
    """`/me` is what the settings field reads. Dropping the claim here is what
    made a saved name invisible."""
    app.dependency_overrides[get_current_user] = lambda: _named("Mojahid")
    r = client.get("/v1/auth/me", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["full_name"] == "Mojahid"


def test_me_without_a_name_is_null_not_missing() -> None:
    app.dependency_overrides[get_current_user] = lambda: _named(None)
    r = client.get("/v1/auth/me", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["full_name"] is None


@pytest.mark.parametrize("name", ["", " ", "a", "x" * 61])
def test_profile_rejects_out_of_range_names(name: str) -> None:
    _wire(_named("unused"))
    r = client.patch("/v1/auth/profile", json={"full_name": name})
    assert r.status_code == 422


def test_profile_trims_surrounding_whitespace() -> None:
    svc = _wire(_named("Mojahid"))
    r = client.patch("/v1/auth/profile", json={"full_name": "  Mojahid  "})
    assert r.status_code == 200
    svc.update_user_metadata.assert_awaited_once_with("fake-token", {"full_name": "Mojahid"})


def test_profile_upstream_failure_is_502() -> None:
    svc = _wire(_named("x"))
    svc.update_user_metadata.side_effect = RuntimeError("gotrue down")
    r = client.patch("/v1/auth/profile", json={"full_name": "Mojahid"})
    assert r.status_code == 502
