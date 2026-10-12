"""Phase 15 Surface 3A — workflow agents (n8n-backed).

Covers the workflow validator, the webhook runtime (MockTransport), and the
service-level invoke error mapping. Endpoint auth is covered by the shared
require_workspace_role tests; registration orchestration is exercised live.
"""

import httpx
import pytest
from fastapi import HTTPException

from sila.adapters.agent_runtime.n8n_workflow import (
    N8nWorkflowRuntime,
    WorkflowNotInvokableError,
)
from sila.core.utils.workflow_validator import (
    find_webhook_path,
    has_respond_node,
    validate_workflow,
)

WEBHOOK_WF = {
    "name": "Social poster",
    "nodes": [
        {"type": "n8n-nodes-base.webhook", "parameters": {"path": "social-post"}},
        {"type": "n8n-nodes-base.respondToWebhook", "parameters": {}},
    ],
}


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def test_validate_accepts_webhook_workflow() -> None:
    definition, errors = validate_workflow(WEBHOOK_WF)
    assert errors == []
    assert find_webhook_path(definition) == "social-post"
    assert has_respond_node(definition)


def test_validate_rejects_non_object() -> None:
    _, errors = validate_workflow(["not", "a", "dict"])
    assert errors and "JSON object" in errors[0]


def test_validate_rejects_no_nodes() -> None:
    _, errors = validate_workflow({"name": "empty", "nodes": []})
    assert errors and "no nodes" in errors[0]


def test_validate_accepts_schedule_or_webhook_trigger() -> None:
    # A schedule trigger is a valid agent trigger (a publisher that posts on its own).
    sched = {"name": "cron only", "nodes": [{"type": "n8n-nodes-base.scheduleTrigger"}]}
    _, errors = validate_workflow(sched)
    assert errors == []
    # But a workflow with NO trigger is rejected.
    none = {"name": "no trigger", "nodes": [{"type": "n8n-nodes-base.httpRequest"}]}
    _, errors = validate_workflow(none)
    assert errors and "trigger" in errors[0].lower()


def test_webhook_id_fallback_when_no_custom_path() -> None:
    wf = {"nodes": [{"type": "n8n-nodes-base.webhook", "parameters": {}, "webhookId": "abc-123"}]}
    assert find_webhook_path(wf) == "abc-123"


# ---------------------------------------------------------------------------
# Runtime (webhook invoke)
# ---------------------------------------------------------------------------


def _runtime_with(handler) -> N8nWorkflowRuntime:  # type: ignore[no-untyped-def]
    """Real runtime with its HTTP client swapped for a MockTransport (same seam
    the Dify adapter tests use) — the actual invoke_workflow code path runs."""
    rt = N8nWorkflowRuntime()
    rt._client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler)
    )
    return rt


@pytest.mark.asyncio
async def test_invoke_returns_json_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/webhook/social-post"
        return httpx.Response(200, json={"posted": True, "id": 7})

    rt = _runtime_with(handler)
    result = await rt.invoke_workflow(WEBHOOK_WF, {"query": "go"})
    assert '"posted": true' in result["answer"]
    assert result["output"] == {"posted": True, "id": 7}
    assert "usage" in result


@pytest.mark.asyncio
async def test_invoke_plain_text_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Workflow was started")

    rt = _runtime_with(handler)
    result = await rt.invoke_workflow(WEBHOOK_WF, {})
    assert result["answer"] == "Workflow was started"


@pytest.mark.asyncio
async def test_invoke_without_webhook_raises() -> None:
    rt = _runtime_with(lambda _: httpx.Response(200))
    with pytest.raises(WorkflowNotInvokableError):
        await rt.invoke_workflow({"nodes": [{"type": "x"}]}, {})


# ---------------------------------------------------------------------------
# Service error mapping (404 webhook = inactive workflow → 409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_maps_webhook_404_to_inactive_409() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from sila.services.agent_service import AgentService

    svc = AgentService.__new__(AgentService)
    version = MagicMock()
    version.dify_dsl = WEBHOOK_WF
    svc._versions = MagicMock()
    svc._versions.get = AsyncMock(return_value=version)

    req = httpx.Request("POST", "http://n8n/webhook/social-post")
    resp = httpx.Response(404, request=req)
    svc._workflow_runtime = MagicMock()
    svc._workflow_runtime.invoke_workflow = AsyncMock(
        side_effect=httpx.HTTPStatusError("404", request=req, response=resp)
    )

    agent = MagicMock()
    with pytest.raises(HTTPException) as exc:
        await svc._invoke_workflow(agent, {"query": "x"})
    assert exc.value.status_code == 409
    assert "activate" in str(exc.value.detail).lower()
