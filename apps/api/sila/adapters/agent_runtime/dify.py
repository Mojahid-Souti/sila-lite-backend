"""DifyAdapter — Dify 1.14 console API at http://dify-api:5001/console/api/*.

Server-to-server access uses Dify's ADMIN_API_KEY: every request carries
``Authorization: Bearer <admin key>`` + ``X-WORKSPACE-ID: <tenant>``, which Dify
authenticates as the workspace owner and exempts from CSRF (see ext_login.py /
token.py in Dify). Both come from settings and are never forwarded to the
frontend.

Validated flow (Phase 14):
  - register: POST /apps/imports (mode=yaml-content) → COMPLETED returns app_id;
    PENDING needs a /confirm. FAILED raises.
  - run: the console debug chat POST /apps/{id}/chat-messages requires the app's
    ``model_config`` in the body, so we GET /apps/{id} first and pass it through.
    Supports response_mode blocking + streaming (Dify-native SSE).
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import yaml

from sila.adapters.agent_runtime.base import AgentDefinitionError
from sila.core.config import settings

logger = logging.getLogger(__name__)

_ALLOWED_MODES = {"chat", "completion", "agent-chat", "workflow", "advanced-chat"}
_TIMEOUT = httpx.Timeout(60.0)
# Streaming: disable the read timeout so a slow LLM token stream isn't killed
# mid-response; keep finite connect/write/pool bounds.
_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)


def _import_error(r: httpx.Response) -> str:
    """Pull Dify's import-failure reason out of a 400 body (best-effort)."""
    try:
        body = r.json()
        return str(body.get("error") or body.get("message") or "invalid Dify DSL")
    except ValueError:
        return "invalid Dify DSL"


class DifyAdapter:
    """Concrete implementation of the AgentRuntime Protocol for Dify 1.14.x."""

    def __init__(self) -> None:
        self._base = settings.dify_base_url.rstrip("/") + "/console/api"
        self._headers = {
            "Authorization": f"Bearer {settings.dify_console_token}",
            "X-WORKSPACE-ID": settings.dify_workspace_id,
            "Content-Type": "application/json",
        }

    def _client(self, timeout: httpx.Timeout = _TIMEOUT) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=timeout)

    async def register_app(self, name: str, dsl: dict[str, Any]) -> str:
        """Import a Dify app from DSL via the console import API. Returns app_id.

        Two-phase: POST /apps/imports (yaml-content). COMPLETED returns the
        app_id directly; PENDING is confirmed with a follow-up call; FAILED
        raises with Dify's reason. O(1) network.
        """
        yaml_content = yaml.safe_dump(dsl, allow_unicode=True, sort_keys=False)
        async with self._client() as client:
            r = await client.post(
                f"{self._base}/apps/imports",
                json={"mode": "yaml-content", "yaml_content": yaml_content, "name": name},
            )
            # 400 = the DSL is invalid (bad definition), not a runtime outage.
            if r.status_code == 400:
                raise AgentDefinitionError(_import_error(r))
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "failed":
                raise AgentDefinitionError(data.get("error") or "import failed")
            app_id = data.get("app_id")
            if data.get("status") == "pending":
                cr = await client.post(
                    f"{self._base}/apps/imports/{data.get('id')}/confirm", json={}
                )
                cr.raise_for_status()
                app_id = cr.json().get("app_id") or app_id
            if not app_id:
                raise RuntimeError("Dify import returned no app_id")
            return str(app_id)

    async def update_app(self, app_id: str, dsl: dict[str, Any]) -> None:
        """Re-import a DSL over an existing app (overwrite in place). O(1)."""
        yaml_content = yaml.safe_dump(dsl, allow_unicode=True, sort_keys=False)
        async with self._client() as client:
            r = await client.post(
                f"{self._base}/apps/imports",
                json={"mode": "yaml-content", "yaml_content": yaml_content, "app_id": app_id},
            )
            r.raise_for_status()

    async def _model_config(self, client: httpx.AsyncClient, app_id: str) -> dict[str, Any]:
        """Fetch the app's model_config — the console chat endpoint requires it
        in the request body (it mirrors the web debug panel)."""
        r = await client.get(f"{self._base}/apps/{app_id}")
        r.raise_for_status()
        mc: dict[str, Any] | None = r.json().get("model_config")
        if not mc:
            raise RuntimeError("Dify app has no model_config")
        return mc

    def _chat_body(
        self, query: str, model_config: dict[str, Any], *, stream: bool
    ) -> dict[str, Any]:
        return {
            "query": query,
            "inputs": {},
            "response_mode": "streaming" if stream else "blocking",
            "conversation_id": "",
            "model_config": model_config,
        }

    async def invoke_sandbox(self, app_id: str, query: str, user_id: str) -> dict[str, Any]:
        """Non-streaming sandbox chat via the console debug endpoint.

        Returns {"answer": str, "usage": {...}}. user_id is unused by the console
        endpoint (auth is the workspace owner) but kept for the Protocol shape.
        """
        async with self._client() as client:
            mc = await self._model_config(client, app_id)
            r = await client.post(
                f"{self._base}/apps/{app_id}/chat-messages",
                json=self._chat_body(query, mc, stream=False),
            )
            r.raise_for_status()
            data = r.json()
            return {"answer": data.get("answer", ""), "usage": data.get("metadata", {})}

    async def stream_sandbox(
        self, app_id: str, query: str, user_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming sandbox chat via the console debug endpoint — relays Dify's
        native SSE. Async-yields ``{"token": chunk}`` then ``{"usage": {...}}``;
        raises on an ``error`` event. O(1) network; LLM-latency bound.
        """
        async with self._client(_STREAM_TIMEOUT) as client:
            mc = await self._model_config(client, app_id)
            async with client.stream(
                "POST",
                f"{self._base}/apps/{app_id}/chat-messages",
                json=self._chat_body(query, mc, stream=True),
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue  # skip SSE comments / ping / blank separators
                    payload = line[len("data:") :].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    etype = event.get("event")
                    if etype in ("message", "agent_message"):
                        chunk = event.get("answer", "")
                        if chunk:
                            yield {"token": chunk}
                    elif etype == "message_end":
                        yield {"usage": event.get("metadata", {})}
                    elif etype == "error":
                        raise RuntimeError(event.get("message", "dify stream error"))

    async def validate_dsl(self, dsl: dict[str, Any]) -> list[str]:
        """Static validation of DSL dict. Returns error list; empty = valid. O(1)."""
        errors: list[str] = []
        app = dsl.get("app", {})
        name = app.get("name", "")
        if not isinstance(name, str) or not (2 <= len(name) <= 100):
            errors.append("app.name must be a string between 2 and 100 characters")
        mode = app.get("mode", "")
        if mode not in _ALLOWED_MODES:
            allowed = sorted(_ALLOWED_MODES)
            errors.append(f"app.mode '{mode}' is not allowed; must be one of {allowed}")
        return errors

    async def delete_app(self, app_id: str) -> None:
        """Delete a Dify app. O(1) — single HTTP DELETE."""
        async with self._client() as client:
            r = await client.delete(f"{self._base}/apps/{app_id}")
            if r.status_code not in (200, 204, 404):
                r.raise_for_status()

    async def list_apps(self) -> list[dict[str, Any]]:
        """List apps in the connected Dify workspace (Build-in-Dify round-trip).

        Returns the lightweight fields the import picker needs. O(1) network
        (single page, capped at 100 — a workspace won't have more in the POC).
        """
        async with self._client() as client:
            r = await client.get(f"{self._base}/apps", params={"page": 1, "limit": 100})
            r.raise_for_status()
            data = r.json().get("data", [])
            return [
                {
                    "id": a.get("id"),
                    "name": a.get("name", ""),
                    "mode": a.get("mode", ""),
                    "description": a.get("description", ""),
                }
                for a in data
                if a.get("id")
            ]

    async def export_app_dsl(self, app_id: str) -> str:
        """Export a Dify app's DSL YAML. Raises httpx errors (404 = app gone). O(1)."""
        async with self._client() as client:
            r = await client.get(f"{self._base}/apps/{app_id}/export")
            r.raise_for_status()
            dsl = r.json().get("data")
            if not isinstance(dsl, str) or not dsl.strip():
                raise RuntimeError("Dify export returned no DSL")
            return dsl
