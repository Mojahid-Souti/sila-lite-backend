"""Workflow-agent runtime — invokes an n8n workflow via its production webhook.

A workflow agent (agents.kind='workflow') wraps an n8n workflow whose entry
point is a Webhook trigger node. Invoke = POST the payload to the trigger's
production URL on the internal Docker network; the workflow's 'Respond to
Webhook' node supplies the reply (n8n acks with a default body otherwise).

Kept deliberately small: no DB access — callers resolve the stored definition
(AgentVersion.dify_dsl holds the workflow JSON) and pass it in.
"""

import json
import time
from typing import Any

import httpx

from sila.core.config import settings
from sila.core.utils.workflow_validator import find_webhook_path


class WorkflowNotInvokableError(Exception):
    """The stored definition has no webhook trigger — cannot be invoked."""


class N8nWorkflowRuntime:
    """Invoke workflow agents over the internal n8n webhook endpoint. O(1) network."""

    def __init__(self) -> None:
        self._base = settings.n8n_internal_url.rstrip("/")
        self._timeout = httpx.Timeout(10.0, read=settings.workflow_invoke_timeout_s)

    def _client(self) -> httpx.AsyncClient:
        # Seam for tests (override with a MockTransport client), mirroring DifyAdapter.
        return httpx.AsyncClient(timeout=self._timeout)

    async def invoke_workflow(
        self, definition: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Run the workflow and return the chat-result shape {answer, usage}.

        The shape matches DifyAdapter.invoke_sandbox so test/public paths can
        treat both kinds uniformly. `usage.latency` is wall-clock seconds.
        """
        path = find_webhook_path(definition)
        if not path:
            raise WorkflowNotInvokableError(
                "Workflow has no webhook trigger — re-publish it with one"
            )
        start = time.perf_counter()
        async with self._client() as client:
            r = await client.post(f"{self._base}/webhook/{path}", json=payload)
            r.raise_for_status()
        latency = time.perf_counter() - start

        try:
            output: Any = r.json()
        except ValueError:
            output = r.text
        answer = (
            output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, indent=2)
        )
        return {
            "answer": answer,
            "output": output,
            "usage": {"usage": {"latency": latency, "total_tokens": 0}},
        }
