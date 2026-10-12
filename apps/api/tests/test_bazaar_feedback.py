"""Bazaar feedback tests — reaction toggle logic, report rate-limit + sanitize,
trending, and auth guards on the new endpoints.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from sila.core.constants import ReactionType
from sila.main import app
from sila.services.bazaar_service import BazaarService, _sanitize_reason

client = TestClient(app)


# --------------------------------------------------------------------------
# Auth guards
# --------------------------------------------------------------------------


def test_trending_unauthenticated() -> None:
    r = client.get(f"/v1/bazaar/trending?workspace_id={uuid.uuid4()}")
    assert r.status_code in (401, 403)


def test_react_unauthenticated() -> None:
    r = client.post(
        f"/v1/bazaar/agents/{uuid.uuid4()}/reactions?workspace_id={uuid.uuid4()}",
        json={"reaction": "like"},
    )
    assert r.status_code in (401, 403)


def test_report_unauthenticated() -> None:
    r = client.post(
        f"/v1/bazaar/agents/{uuid.uuid4()}/reports?workspace_id={uuid.uuid4()}",
        json={"reason": "spam"},
    )
    assert r.status_code in (401, 403)


# --------------------------------------------------------------------------
# Reason sanitisation
# --------------------------------------------------------------------------


def test_sanitize_reason_strips_html_and_truncates() -> None:
    assert _sanitize_reason("<b>bad</b> agent") == "bad agent"
    assert len(_sanitize_reason("x" * 5000)) == 2000
    assert _sanitize_reason("   ") == ""


# --------------------------------------------------------------------------
# Reaction toggle logic + counter maintenance
# --------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self) -> None:
        self.liked_count = 0
        self.disliked_count = 0
        self.report_count = 0


def _svc_with_agent(agent: _FakeAgent, existing_reaction: object | None) -> BazaarService:
    svc = BazaarService(AsyncMock(), MagicMock())
    svc._bazaar = MagicMock()
    svc._bazaar.get_published = AsyncMock(return_value=agent)
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing_reaction
    svc._db.execute = AsyncMock(return_value=result)
    svc._db.commit = AsyncMock()
    svc._db.add = MagicMock()
    svc._db.delete = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_like_increments_counter() -> None:
    agent = _FakeAgent()
    svc = _svc_with_agent(agent, existing_reaction=None)
    out = await svc.react(
        workspace_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        reaction=ReactionType.LIKE,
    )
    assert out["liked_count"] == 1
    assert out["my_reaction"] == "like"
    svc._db.add.assert_called_once()


@pytest.mark.asyncio
async def test_clicking_like_again_toggles_off() -> None:
    agent = _FakeAgent()
    agent.liked_count = 1
    existing = MagicMock()
    existing.reaction = "like"
    svc = _svc_with_agent(agent, existing_reaction=existing)
    out = await svc.react(
        workspace_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        reaction=ReactionType.LIKE,
    )
    assert out["liked_count"] == 0
    assert out["my_reaction"] is None
    svc._db.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_switch_like_to_dislike() -> None:
    agent = _FakeAgent()
    agent.liked_count = 1
    existing = MagicMock()
    existing.reaction = "like"
    svc = _svc_with_agent(agent, existing_reaction=existing)
    out = await svc.react(
        workspace_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        reaction=ReactionType.DISLIKE,
    )
    assert out["liked_count"] == 0
    assert out["disliked_count"] == 1
    assert out["my_reaction"] == "dislike"


# --------------------------------------------------------------------------
# Report rate-limit
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_rate_limit_blocks_after_max() -> None:
    agent = _FakeAgent()
    svc = BazaarService(AsyncMock(), MagicMock())
    svc._bazaar = MagicMock()
    svc._bazaar.get_published = AsyncMock(return_value=agent)
    result = MagicMock()
    result.scalar_one.return_value = 5  # already at the daily cap
    svc._db.execute = AsyncMock(return_value=result)

    with pytest.raises(HTTPException) as exc:
        await svc.report(
            workspace_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            reporter_id=uuid.uuid4(),
            reason="spam",
            background_tasks=MagicMock(),
        )
    assert exc.value.status_code == 429
