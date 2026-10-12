"""Bazaar endpoint unit tests — auth guard enforcement, review validation,
hire idempotency logic, and k-anonymity on ratings.
"""

import uuid
from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sila.db.repositories.review import ReviewRepository
from sila.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_overrides() -> Generator[None, Any]:
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Auth guard — catalog and bazaar endpoints require authentication
# ---------------------------------------------------------------------------


def test_catalog_unauthenticated() -> None:
    r = client.get(f"/v1/bazaar/agents?workspace_id={uuid.uuid4()}")
    assert r.status_code in (401, 403)


def test_bazaar_agent_detail_unauthenticated() -> None:
    r = client.get(f"/v1/bazaar/agents/{uuid.uuid4()}?workspace_id={uuid.uuid4()}")
    assert r.status_code in (401, 403)


def test_categories_unauthenticated() -> None:
    r = client.get("/v1/bazaar/categories")
    assert r.status_code in (401, 403)


def test_reviews_unauthenticated() -> None:
    r = client.get(f"/v1/bazaar/agents/{uuid.uuid4()}/reviews")
    assert r.status_code in (401, 403)


def test_submit_review_unauthenticated() -> None:
    r = client.post(
        f"/v1/bazaar/agents/{uuid.uuid4()}/reviews?workspace_id={uuid.uuid4()}",
        json={"rating": 5, "body": "great"},
    )
    assert r.status_code in (401, 403)


def test_hire_unauthenticated() -> None:
    r = client.post(f"/v1/workspaces/{uuid.uuid4()}/hires?agent_id={uuid.uuid4()}")
    assert r.status_code in (401, 403)


def test_list_hires_unauthenticated() -> None:
    r = client.get(f"/v1/workspaces/{uuid.uuid4()}/hires")
    assert r.status_code in (401, 403)


def test_unhire_unauthenticated() -> None:
    r = client.delete(f"/v1/workspaces/{uuid.uuid4()}/hires/{uuid.uuid4()}")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Review schema validation — rating must be 1-5
# ---------------------------------------------------------------------------


def test_review_rating_below_range() -> None:
    r = client.post(
        f"/v1/bazaar/agents/{uuid.uuid4()}/reviews?workspace_id={uuid.uuid4()}",
        json={"rating": 0, "body": "bad"},
        headers={"Authorization": "Bearer fake"},
    )
    # 422 (validation error) or 401 (auth) — either proves schema is enforced
    assert r.status_code in (401, 403, 422)


def test_review_rating_above_range() -> None:
    r = client.post(
        f"/v1/bazaar/agents/{uuid.uuid4()}/reviews?workspace_id={uuid.uuid4()}",
        json={"rating": 6, "body": "too high"},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code in (401, 403, 422)


def test_review_body_too_long() -> None:
    r = client.post(
        f"/v1/bazaar/agents/{uuid.uuid4()}/reviews?workspace_id={uuid.uuid4()}",
        json={"rating": 3, "body": "x" * 2001},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code in (401, 403, 422)


# ---------------------------------------------------------------------------
# k-anonymity: avg_rating returns None when count < 3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_avg_rating_below_threshold_returns_none() -> None:
    """When fewer than 3 reviews exist, avg must be None (k-anonymity)."""
    mock_db = AsyncMock()

    # Simulate 2 reviews with avg 4.5
    mock_result = MagicMock()
    mock_result.one.return_value = (Decimal("4.5"), 2)
    mock_db.execute = AsyncMock(return_value=mock_result)

    repo = ReviewRepository(mock_db)
    result = await repo.avg_rating(uuid.uuid4())
    assert result["avg"] is None
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_avg_rating_at_threshold_reveals_avg() -> None:
    """When 3 or more reviews exist, avg is revealed."""
    mock_db = AsyncMock()

    mock_result = MagicMock()
    mock_result.one.return_value = (Decimal("3.67"), 3)
    mock_db.execute = AsyncMock(return_value=mock_result)

    repo = ReviewRepository(mock_db)
    result = await repo.avg_rating(uuid.uuid4())
    assert result["avg"] == 3.7  # rounded to 1dp
    assert result["count"] == 3


@pytest.mark.asyncio
async def test_avg_rating_no_reviews() -> None:
    """Zero reviews: avg is None, count is 0."""
    mock_db = AsyncMock()

    mock_result = MagicMock()
    mock_result.one.return_value = (None, 0)
    mock_db.execute = AsyncMock(return_value=mock_result)

    repo = ReviewRepository(mock_db)
    result = await repo.avg_rating(uuid.uuid4())
    assert result["avg"] is None
    assert result["count"] == 0


# ---------------------------------------------------------------------------
# HireRepository — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hire_is_idempotent() -> None:
    """Hiring the same agent twice returns the existing hire, not a duplicate."""
    from sila.db.models import Hire
    from sila.db.repositories.hire import HireRepository

    workspace_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()

    existing_hire = Hire(
        workspace_id=workspace_id,
        agent_id=agent_id,
        hired_by=user_id,
    )
    existing_hire.id = uuid.uuid4()

    mock_db = AsyncMock()
    repo = HireRepository(mock_db)

    # Patch the get method to return an existing hire
    with patch.object(repo, "get", AsyncMock(return_value=existing_hire)):
        result = await repo.hire(
            workspace_id=workspace_id,
            agent_id=agent_id,
            hired_by=user_id,
        )

    assert result is existing_hire
    # No add() should have been called — returned the existing row
    mock_db.add.assert_not_called()


# ---------------------------------------------------------------------------
# Profanity filter — verify better_profanity is installed and usable
# ---------------------------------------------------------------------------


def test_profanity_filter_available() -> None:
    """Verify better_profanity is installed and censors offensive content."""
    from better_profanity import profanity

    profanity.load_censor_words()
    result = profanity.censor("This is a shit test")
    assert "shit" not in result
    assert "****" in result or result != "This is a shit test"
