"""Phase 17 onboarding endpoint + experience→mode mapping tests.

All external calls (GoTrue, DB) are mocked via dependency_overrides — no network,
no DB. Covers the happy path for both tiers, the audit write, and enum rejection.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sila.adapters.auth.base import UserResult
from sila.api.deps import get_access_token, get_auth_service, get_current_user
from sila.core.constants import AgentExperience, UxMode, ux_mode_for_experience
from sila.db.session import get_db
from sila.main import app

client = TestClient(app)

_USER = UserResult(id=uuid.uuid4(), email="u@example.com", email_confirmed=True)


@pytest.fixture(autouse=True)
def clear_overrides() -> Generator[None]:
    yield
    app.dependency_overrides.clear()


def _wire(updated: UserResult) -> AsyncMock:
    """Override auth (returns the post-write user), the caller identity, the raw
    token, and a DB session (so the audit write runs without a real database)."""
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


def _user(mode: str) -> UserResult:
    return UserResult(
        id=_USER.id,
        email=_USER.email,
        email_confirmed=True,
        onboarding_complete=True,
        ux_mode=mode,
    )


@pytest.mark.parametrize(
    ("experience", "expected"),
    [
        (AgentExperience.NEVER, UxMode.SIMPLE),
        (AgentExperience.SOME, UxMode.SIMPLE),
        (AgentExperience.EXPERIENCED, UxMode.ADVANCED),
    ],
)
def test_experience_maps_to_mode(experience: AgentExperience, expected: UxMode) -> None:
    assert ux_mode_for_experience(experience) is expected


def test_onboarding_experienced_routes_advanced() -> None:
    svc = _wire(_user("advanced"))
    r = client.patch(
        "/v1/auth/onboarding",
        json={
            "referral_source": "linkedin",
            "job_role": "developer",
            "agent_experience": "experienced",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["onboarding_complete"] is True
    assert body["ux_mode"] == "advanced"
    # The server persisted onboarding_complete + the server-computed mode.
    written = svc.update_user_metadata.await_args.args[1]
    assert written["onboarding_complete"] is True
    assert written["ux_mode"] == "advanced"


def test_onboarding_beginner_routes_simple() -> None:
    _wire(_user("simple"))
    r = client.patch(
        "/v1/auth/onboarding",
        json={
            "referral_source": "colleague",
            "job_role": "manager",
            "agent_experience": "never",
        },
    )
    assert r.status_code == 200
    assert r.json()["ux_mode"] == "simple"


def test_onboarding_rejects_off_enum_value() -> None:
    _wire(_user("simple"))
    r = client.patch(
        "/v1/auth/onboarding",
        json={
            "referral_source": "telegram",  # not in the enum
            "job_role": "developer",
            "agent_experience": "experienced",
        },
    )
    assert r.status_code == 422


def test_onboarding_other_with_text_is_stripped_and_stored() -> None:
    svc = _wire(_user("simple"))
    r = client.patch(
        "/v1/auth/onboarding",
        json={
            "referral_source": "other",
            "referral_source_other": "  Friend at a meetup  ",
            "job_role": "other",
            "job_role_other": "Founder",
            "agent_experience": "never",
        },
    )
    assert r.status_code == 200
    written = svc.update_user_metadata.await_args.args[1]
    assert written["referral_source_other"] == "Friend at a meetup"
    assert written["job_role_other"] == "Founder"


def test_onboarding_other_without_text_is_422() -> None:
    _wire(_user("simple"))
    r = client.patch(
        "/v1/auth/onboarding",
        json={"referral_source": "other", "job_role": "manager", "agent_experience": "never"},
    )
    assert r.status_code == 422


def test_onboarding_other_text_too_long_is_422() -> None:
    _wire(_user("simple"))
    r = client.patch(
        "/v1/auth/onboarding",
        json={
            "referral_source": "other",
            "referral_source_other": "x" * 61,
            "job_role": "manager",
            "agent_experience": "never",
        },
    )
    assert r.status_code == 422
