"""The skills and notifications API: signed-in only, roles from OPA, own rows only."""

import pathlib
import re
import typing
import uuid
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from sila.adapters.auth.base import UserResult
from sila.api import deps
from sila.api.v1 import skills as skills_api
from sila.core.constants import ROLE_RANK, Role
from sila.main import app
from sila.services.notification_service import NotificationKind, notify

client = TestClient(app)
WS, SKILL_ID = uuid.uuid4(), uuid.uuid4()
USER = UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True)
REGO = pathlib.Path(__file__).parents[3] / "infra/opa/policies/authz.rego"


def _min_roles() -> dict[str, str]:
    """The policy's own table, so the test and OPA cannot drift apart."""
    return dict(re.findall(r'"(skill\.[a-z]+)":\s*"([a-z]+)"', REGO.read_text()))


@pytest.fixture(autouse=True)
def clear_overrides() -> Generator[None, Any]:
    yield
    app.dependency_overrides.clear()


def test_the_policy_sets_who_does_what() -> None:
    assert _min_roles() == {
        "skill.import": "admin",
        "skill.approve": "reviewer",
        "skill.reject": "reviewer",
        "skill.rescan": "reviewer",
        "skill.publish": "reviewer",
        "skill.unpublish": "reviewer",
        "skill.remove": "admin",
        "skill.attach": "viewer",
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/bazaar/skills"),
        ("get", f"/v1/bazaar/skills/{SKILL_ID}"),
        ("get", f"/v1/workspaces/{WS}/skills/catalog"),
        ("post", f"/v1/workspaces/{WS}/skills/import"),
        ("get", f"/v1/workspaces/{WS}/skills"),
        ("post", f"/v1/workspaces/{WS}/skills/{SKILL_ID}/approve"),
        ("get", "/v1/notifications"),
        ("post", "/v1/notifications/read-all"),
        ("post", f"/v1/notifications/{SKILL_ID}/read"),
    ],
)
def test_every_route_needs_a_signed_in_user(method: str, path: str) -> None:
    body = {"json": {"source": "a/b", "skill_key": "c"}} if method == "post" else {}
    response = getattr(client, method)(path, **body)
    assert response.status_code in (401, 403)


class _Service:
    calls: list[str] = []

    async def import_skill(self, **kw: Any) -> Any:
        _Service.calls.append("import")
        return _skill()

    async def approve(self, *_a: Any) -> Any:
        _Service.calls.append("approve")
        return _skill()

    async def remove(self, *_a: Any) -> None:
        _Service.calls.append("remove")


def _skill() -> Any:
    from sila.db.models import Skill

    return Skill(
        id=SKILL_ID,
        workspace_id=WS,
        source="a/b",
        skill_key="c",
        name="c",
        description="",
        category="other",
        repo_url="https://github.com/a/b",
        license=None,
        installs=0,
        has_scripts=False,
        dependencies=[],
        body="x",
        body_sha256="0" * 64,
        vetting_status="needs_review",
        findings=[],
        published=False,
    )


def _as(role: Role, monkeypatch) -> None:
    member = typing.get_args(skills_api.Member)[1].dependency
    app.dependency_overrides[member] = lambda: (USER, role)
    app.dependency_overrides[skills_api.get_skill_service] = _Service
    minimum = _min_roles()

    async def _opa(action: str, caller: Role) -> bool:
        return ROLE_RANK[caller] >= ROLE_RANK[Role(minimum[str(action)])]

    monkeypatch.setattr(skills_api, "check_opa", _opa)
    _Service.calls = []


@pytest.mark.parametrize(("role", "allowed"), [(Role.VIEWER, False), (Role.ADMIN, True)])
def test_only_an_admin_imports(role: Role, allowed: bool, monkeypatch) -> None:
    _as(role, monkeypatch)
    r = client.post(f"/v1/workspaces/{WS}/skills/import", json={"source": "a/b", "skill_key": "c"})
    assert (r.status_code == 201) is allowed
    assert _Service.calls == (["import"] if allowed else [])


@pytest.mark.parametrize(("role", "allowed"), [(Role.HIRER, False), (Role.REVIEWER, True)])
def test_a_reviewer_decides(role: Role, allowed: bool, monkeypatch) -> None:
    _as(role, monkeypatch)
    r = client.post(f"/v1/workspaces/{WS}/skills/{SKILL_ID}/approve")
    assert (r.status_code == 200) is allowed


@pytest.mark.parametrize(("role", "allowed"), [(Role.REVIEWER, False), (Role.ADMIN, True)])
def test_only_an_admin_removes(role: Role, allowed: bool, monkeypatch) -> None:
    _as(role, monkeypatch)
    r = client.delete(f"/v1/workspaces/{WS}/skills/{SKILL_ID}")
    assert r.status_code == (204 if allowed else 403)
    assert _Service.calls == (["remove"] if allowed else [])


def test_only_the_four_decisions_exist(monkeypatch) -> None:
    _as(Role.ADMIN, monkeypatch)
    assert client.post(f"/v1/workspaces/{WS}/skills/{SKILL_ID}/delete").status_code == 422


# --- notifications -----------------------------------------------------------------


async def test_notify_never_raises_and_keeps_links_internal() -> None:
    db = MagicMock()
    added: list[Any] = []
    db.add = added.append
    db.begin_nested = MagicMock(return_value=AsyncMock())
    for link, kept in [("/bazaar", "/bazaar"), ("//evil.com", None), ("https://x.y", None)]:
        await notify(db, user_id=USER.id, kind=NotificationKind.AGENT_HIRED, title="t", link=link)
        assert added[-1].link == kept
    await notify(db, user_id=USER.id, kind=NotificationKind.AGENT_HIRED, title="a\u202eb\x07")
    assert added[-1].title == "ab"

    failing = MagicMock()
    failing.begin_nested = MagicMock(side_effect=SQLAlchemyError("db down"))
    await notify(failing, user_id=USER.id, kind=NotificationKind.AGENT_HIRED, title="t")


class _Notes:
    marked: list[tuple[uuid.UUID, uuid.UUID]] = []

    def __init__(self, _db: object) -> None: ...

    async def latest(self, user_id: uuid.UUID, *, limit: int) -> list[Any]:
        return []

    async def unread_count(self, user_id: uuid.UUID) -> int:
        return 3

    async def mark_read(self, user_id: uuid.UUID, notification_id: uuid.UUID) -> bool:
        _Notes.marked.append((user_id, notification_id))
        return False

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        return 3


def test_the_bell_reads_and_marks_only_the_callers_rows(monkeypatch) -> None:
    from sila.db.session import get_db

    app.dependency_overrides[deps.get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    monkeypatch.setattr("sila.api.v1.notifications.NotificationRepository", _Notes)
    _Notes.marked = []
    assert client.get("/v1/notifications").json() == {"items": [], "unread": 3}
    someone_elses = uuid.uuid4()
    # Idempotent, and an id that is not the caller's reveals nothing.
    assert client.post(f"/v1/notifications/{someone_elses}/read").status_code == 204
    assert _Notes.marked == [(USER.id, someone_elses)]
    assert client.post("/v1/notifications/read-all").json() == {"marked": 3}


async def test_hiring_an_agent_rings_the_bell(monkeypatch) -> None:
    from sila.services.bazaar_service import BazaarService

    notes: list[dict[str, Any]] = []

    async def _notify(_db: object, **kw: Any) -> None:
        notes.append(kw)

    monkeypatch.setattr("sila.services.bazaar_service.notify", _notify)
    svc = BazaarService(AsyncMock(), AsyncMock())
    svc._bazaar = AsyncMock()
    svc._bazaar.get_published.return_value = MagicMock(name="agent")
    svc._bazaar.get_published.return_value.name = "Leave Helper"
    svc._hires = AsyncMock()
    svc._hires.hire.return_value = MagicMock(id=uuid.uuid4())
    await svc.hire(
        workspace_id=WS, agent_id=uuid.uuid4(), hired_by=USER.id, background_tasks=MagicMock()
    )
    assert notes[0]["kind"] == NotificationKind.AGENT_HIRED
    assert notes[0]["title"] == "Agent hired: Leave Helper"


def test_timestamps_reach_the_browser_as_utc() -> None:
    """A zone-less ISO string is read as local time by a browser."""
    from datetime import UTC, datetime, timedelta, timezone

    from sila.core.utils.timestamps import utc_iso

    assert utc_iso(datetime(2026, 9, 28, 7, 14)) == "2026-09-28T07:14:00+00:00"
    muscat = datetime(2026, 9, 28, 11, 14, tzinfo=timezone(timedelta(hours=4)))
    assert utc_iso(muscat) == "2026-09-28T07:14:00+00:00"
    assert utc_iso(datetime(2026, 1, 1, tzinfo=UTC)).endswith("+00:00")
