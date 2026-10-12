"""Team sharing (run-on-owner's-creds): the owner-binding pre-check, the OPA
activation row, and the auth guard on the activation endpoint."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sila.main import app

client = TestClient(app)


class _Bindings:
    """Only `bound` providers have a connection for the looked-up user."""

    def __init__(self, _db: Any, bound: set[str] | None = None) -> None:
        self.bound = bound if bound is not None else set()

    async def get(self, *, user_id: Any, workspace_id: Any, provider: str) -> Any:
        return SimpleNamespace(nango_connection_id="c1") if provider in self.bound else None


@pytest.mark.asyncio
async def test_missing_oauth_bindings_names_unbound_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sila.services.sila import workflow_assembly as wa

    monkeypatch.setattr(wa, "ConnectorBindingRepository", lambda db: _Bindings(db, bound={"slack"}))
    missing = await wa.missing_oauth_bindings(
        db=None,
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        destinations=[
            {"type": "slack", "channel_id": "C1"},  # bound → fine
            {"type": "gmail", "to": "a@b.om"},  # oauth, unbound → named
            {"type": "email", "to": "a@b.om"},  # platform email, not oauth → skipped
            {"type": "gmail", "to": "c@d.om"},  # duplicate provider → named once
        ],
    )
    assert missing == ["Gmail"]


@pytest.mark.asyncio
async def test_missing_oauth_bindings_empty_when_all_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sila.services.sila import workflow_assembly as wa

    monkeypatch.setattr(
        wa, "ConnectorBindingRepository", lambda db: _Bindings(db, bound={"slack", "gmail"})
    )
    missing = await wa.missing_oauth_bindings(
        db=None,
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        destinations=[{"type": "slack"}, {"type": "gmail"}],
    )
    assert missing == []


def test_opa_matrix_has_hirer_activation() -> None:
    rego = (
        Path(__file__).resolve().parents[2] / ".." / "infra" / "opa" / "policies" / "authz.rego"
    ).resolve()
    text = rego.read_text()
    assert '"agent.activate":    "hirer"' in text


def test_activation_endpoint_requires_auth() -> None:
    r = client.post(
        f"/v1/workspaces/{uuid.uuid4()}/agents/{uuid.uuid4()}/activation",
        json={"active": True},
    )
    assert r.status_code == 401
