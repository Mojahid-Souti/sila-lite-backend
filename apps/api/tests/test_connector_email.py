"""Action-completeness: Slack auto-join + SMTP email-send (so the connector path
works for any user, not just a manually-invited owner bot)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException

from sila.adapters.connectors.nango_client import NangoError
from sila.services.connectors.connector_service import ConnectorError, ConnectorService


class _StubNango:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def proxy(
        self,
        *,
        connection_id: str,
        provider_config_key: str,
        method: str,
        endpoint: str,
        json: Any = None,
    ) -> dict[str, Any]:
        self.calls.append(endpoint)
        r = self.responses.get(endpoint, {"ok": True})
        if isinstance(r, Exception):
            raise r
        return r


def _svc(responses: dict[str, Any]) -> ConnectorService:
    return ConnectorService(db=None, nango=_StubNango(responses))  # type: ignore[arg-type]


# --- A: Slack auto-join ----------------------------------------------------


async def test_autojoin_ok() -> None:
    svc = _svc({"conversations.join": {"ok": True}})
    await svc._slack_ensure_member("c1", "slack", "C123")
    assert svc._nango.calls == ["conversations.join"]  # type: ignore[attr-defined]


async def test_autojoin_idempotent_already_member() -> None:
    # already_in_channel must NOT raise — the post should proceed.
    svc = _svc({"conversations.join": {"ok": False, "error": "already_in_channel"}})
    await svc._slack_ensure_member("c1", "slack", "C123")


async def test_autojoin_private_channel_actionable_error() -> None:
    svc = _svc(
        {"conversations.join": {"ok": False, "error": "method_not_supported_for_channel_type"}}
    )
    with pytest.raises(ConnectorError) as e:
        await svc._slack_ensure_member("c1", "slack", "C999")
    msg = str(e.value).lower()
    assert "private" in msg or "invite" in msg  # clear, not a raw not_in_channel


async def test_autojoin_missing_scope_actionable_error() -> None:
    svc = _svc({"conversations.join": {"ok": False, "error": "missing_scope"}})
    with pytest.raises(ConnectorError) as e:
        await svc._slack_ensure_member("c1", "slack", "C123")
    assert "channels:join" in str(e.value)


async def test_autojoin_best_effort_on_transport_error() -> None:
    # A Nango transport error must not block the post (it fails clearly downstream).
    svc = _svc({"conversations.join": NangoError("boom")})
    await svc._slack_ensure_member("c1", "slack", "C123")


async def test_autojoin_noop_without_channel() -> None:
    svc = _svc({})
    await svc._slack_ensure_member("c1", "slack", "")
    assert svc._nango.calls == []  # type: ignore[attr-defined]


# --- B: email capability + send-email gating -------------------------------


def test_email_capability_roundtrip_and_isolation() -> None:
    from sila.services.connectors import capability as cc
    from sila.services.sila import email_capability as ec

    ws, uid = uuid.uuid4(), uuid.uuid4()
    tok = ec.mint(workspace_id=ws, user_id=uid, to="me@x.dev")
    cap = ec.verify(tok)
    assert cap and cap.workspace_id == ws and cap.user_id == uid and cap.to == "me@x.dev"
    assert ec.verify(tok[:-3] + "zzz") is None  # tampered → fail-closed
    # A connector token must not verify as an email capability (scope-isolated).
    other = cc.mint(
        workspace_id=ws,
        user_id=uid,
        provider="slack",
        endpoint="chat.postMessage",
        connection_id="c1",
    )
    assert ec.verify(other) is None


async def test_send_email_rejects_invalid_capability() -> None:
    from sila.api.v1.internal_runs import SendEmailRequest, internal_send_email

    with pytest.raises(HTTPException) as e:
        await internal_send_email(
            SendEmailRequest(subject="x", body="y"),
            db=None,  # type: ignore[arg-type]
            x_sila_email_token="garbage",
        )
    assert e.value.status_code == 403  # capability-gated before any send / DB touch
