"""The Lite edition's switches: AI off, sign-up by domain, Omantel parts optional."""

import importlib
import sys
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from sila.api import deps
from sila.api.v1 import auth as auth_api
from sila.core.config import settings
from sila.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides() -> Any:  # noqa: ANN401
    yield
    app.dependency_overrides.clear()


# --- AI off ----------------------------------------------------------------------


async def test_ai_endpoints_refuse_with_the_message_while_ai_is_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "sila_ai_enabled", False)
    with pytest.raises(HTTPException) as caught:
        await deps.require_ai_enabled()
    assert caught.value.status_code == 503
    assert caught.value.detail == settings.sila_ai_unavailable_message


async def test_ai_endpoints_pass_while_ai_is_on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "sila_ai_enabled", True)
    assert await deps.require_ai_enabled() is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/workspaces/00000000-0000-0000-0000-000000000001/skills/import"),
        (
            "post",
            "/v1/workspaces/00000000-0000-0000-0000-000000000001/agents/"
            "00000000-0000-0000-0000-000000000002/submit-for-review",
        ),
    ],
)
def test_scans_say_the_ai_is_waiting(method: str, path: str, monkeypatch) -> None:
    """Nothing starts a scan that would fail closed half-way: the route says why."""
    monkeypatch.setattr(settings, "sila_ai_enabled", False)
    response = getattr(client, method)(path, json={"source": "a/b", "skill_key": "c"})
    assert response.status_code == 503
    assert response.json()["detail"] == settings.sila_ai_unavailable_message


# --- sign-up by domain -------------------------------------------------------------


@pytest.mark.parametrize(
    ("allowed", "email", "ok"),
    [
        ("", "anyone@example.com", True),
        ("omantel.om", "staff@omantel.om", True),
        ("omantel.om", "Staff@OMANTEL.OM", True),
        ("@omantel.om, other.org", "x@other.org", True),
        ("omantel.om", "x@gmail.com", False),
        ("omantel.om", "x@mail.omantel.om", False),
        ("omantel.om", "x@omantel.om.evil.com", False),
    ],
)
def test_signup_domain_rule(allowed: str, email: str, ok: bool, monkeypatch) -> None:
    monkeypatch.setattr(settings, "sila_signup_allowed_domains", allowed)
    assert auth_api.signup_domain_allowed(email) is ok


def test_signup_outside_the_domain_never_reaches_the_auth_service(monkeypatch) -> None:
    monkeypatch.setattr(settings, "sila_signup_allowed_domains", "omantel.om")

    class _Auth:
        async def sign_up(self, *_a: Any, **_k: Any) -> Any:  # noqa: ANN401
            raise AssertionError("must not be called")

    app.dependency_overrides[deps.get_auth_service] = _Auth
    response = client.post(
        "/v1/auth/signup",
        json={
            "email": "someone@gmail.com",
            "password": "Correct-Horse-9-Battery",
            "confirm_password": "Correct-Horse-9-Battery",
            "full_name": "Some One",
        },
    )
    assert response.status_code == 403
    assert "organisation" in response.json()["detail"]


# --- Omantel-only parts are optional ------------------------------------------------


def test_the_catalogue_lists_the_generic_templates_without_the_omantel_file(
    monkeypatch,
) -> None:
    from sila.services import bazaar_templates

    before = list(bazaar_templates.TEMPLATES)
    monkeypatch.setitem(sys.modules, "sila.services.bazaar_templates_omantel", None)
    try:
        reloaded = importlib.reload(bazaar_templates)
        assert list(reloaded.TEMPLATES) == ["tpl-data-analyst", "tpl-pdd"]
        assert reloaded.persona_for(reloaded.OKR_TEMPLATE_ID) == ""
    finally:
        monkeypatch.delitem(sys.modules, "sila.services.bazaar_templates_omantel")
        importlib.reload(bazaar_templates)
    assert list(bazaar_templates.TEMPLATES) == before


def test_without_the_okr_pack_nothing_is_an_okr_sheet(monkeypatch) -> None:
    from sila.services import okr_optional

    before = okr_optional.OKR_AVAILABLE
    monkeypatch.setitem(sys.modules, "sila.services.okr.detect", None)
    try:
        reloaded = importlib.reload(okr_optional)
        assert reloaded.OKR_AVAILABLE is False
        assert reloaded.is_okr_csv("#,Objective,Key Result") is False
        assert reloaded.is_okr_workbook(b"PK") is False
        assert reloaded.contains_okr_header("anything") is False
    finally:
        monkeypatch.delitem(sys.modules, "sila.services.okr.detect")
        importlib.reload(okr_optional)
    assert okr_optional.OKR_AVAILABLE is before
