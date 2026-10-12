"""Phase 15 — Build-in-Dify round-trip.

Covers the DifyAdapter list/export calls, AgentService.import_from_dify_app error
mapping + orchestration (it reuses import_from_yaml), and the endpoint auth guards.
"""

import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from sila.adapters.agent_runtime.dify import DifyAdapter
from sila.main import app
from sila.services.agent_service import AgentService

client = TestClient(app)


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> DifyAdapter:
    a = DifyAdapter()
    a._client = lambda *_a, **_k: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0)
    )
    return a


# --- DifyAdapter.list_apps -------------------------------------------------


async def test_list_apps_parses_lightweight_fields() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "a1", "name": "HR Bot", "mode": "chat", "description": "d", "x": 1},
                    {"id": "a2", "name": "Sales", "mode": "agent-chat", "description": ""},
                    {"name": "no-id"},  # dropped — no id
                ]
            },
        )

    apps = await _adapter(handler).list_apps()
    assert [a["id"] for a in apps] == ["a1", "a2"]
    assert apps[0] == {"id": "a1", "name": "HR Bot", "mode": "chat", "description": "d"}


# --- DifyAdapter.export_app_dsl --------------------------------------------


async def test_export_app_dsl_returns_yaml() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": "app:\n  name: X\n"})

    assert (await _adapter(handler).export_app_dsl("a1")).startswith("app:")


async def test_export_app_dsl_raises_on_empty() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    with pytest.raises(RuntimeError):
        await _adapter(handler).export_app_dsl("a1")


# --- AgentService.import_from_dify_app --------------------------------------


async def test_import_from_dify_app_maps_404() -> None:
    svc = AgentService(db=MagicMock(), runtime=AsyncMock())
    req = httpx.Request("GET", "http://dify/apps/a1/export")
    svc._runtime.export_app_dsl = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "nope", request=req, response=httpx.Response(404, request=req)
        )
    )
    with pytest.raises(HTTPException) as ei:
        await svc.import_from_dify_app(
            workspace_id=uuid.uuid4(), owner_id=uuid.uuid4(), dify_app_id="a1"
        )
    assert ei.value.status_code == 404


async def test_import_from_dify_app_maps_connect_error_to_502() -> None:
    svc = AgentService(db=MagicMock(), runtime=AsyncMock())
    svc._runtime.export_app_dsl = AsyncMock(side_effect=httpx.ConnectError("down"))  # type: ignore[method-assign]
    with pytest.raises(HTTPException) as ei:
        await svc.import_from_dify_app(
            workspace_id=uuid.uuid4(), owner_id=uuid.uuid4(), dify_app_id="a1"
        )
    assert ei.value.status_code == 502


async def test_import_from_dify_app_reuses_yaml_import() -> None:
    svc = AgentService(db=MagicMock(), runtime=AsyncMock())
    svc._runtime.export_app_dsl = AsyncMock(return_value="app:\n  name: X\n")  # type: ignore[method-assign]
    svc.import_from_yaml = AsyncMock(return_value="AGENT")  # type: ignore[method-assign]

    out = await svc.import_from_dify_app(
        workspace_id=uuid.uuid4(), owner_id=uuid.uuid4(), dify_app_id="abc"
    )

    assert out == "AGENT"
    kwargs = svc.import_from_yaml.call_args.kwargs
    assert kwargs["raw_yaml"] == b"app:\n  name: X\n"
    assert kwargs["filename"] == "dify-abc.yaml"


# --- Auth guards ------------------------------------------------------------


def test_list_dify_apps_unauthenticated() -> None:
    r = client.get(f"/v1/workspaces/{uuid.uuid4()}/dify-apps")
    assert r.status_code in (401, 403)


def test_import_from_dify_unauthenticated() -> None:
    r = client.post(
        f"/v1/workspaces/{uuid.uuid4()}/agents/import-from-dify", json={"dify_app_id": "x"}
    )
    assert r.status_code in (401, 403)
