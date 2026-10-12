"""HTTP MCP client — Streamable HTTP transport (MCP 2025-06-18).

Spec: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports

Speaks to both server styles through one client:
  - Full Streamable HTTP (DeepWiki, Context7, HuggingFace, …): `initialize`
    handshake → `Mcp-Session-Id` → JSON *or* SSE (`text/event-stream`) responses.
  - Minimal JSON-RPC-over-POST (Sila built-ins): no handshake, plain JSON.

The handshake is best-effort: if `initialize` isn't supported we fall back to
direct calls, so both styles work without the caller knowing which is which.

Errors raise MCPError; callers map them to ToolInvocation rows with
outcome=failure/timeout. Per public call: O(1) network — a fixed handshake
plus one method call over a single keep-alive connection.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .base import MCPClient, MCPImageBlock, MCPInvocationResult, MCPToolDef

DEFAULT_TIMEOUT_S = 30.0
_PROTOCOL_VERSION = "2025-06-18"
_ACCEPT = "application/json, text/event-stream"
_CLIENT_INFO = {"name": "sila", "version": "1.0"}


class MCPError(Exception):
    """Raised on MCP protocol failures (transport, JSON-RPC error, malformed response)."""


@dataclass(frozen=True)
class _Session:
    """Outcome of the (best-effort) initialize handshake.

    `handshook` = the server accepted `initialize` (full transport).
    `id` = the `Mcp-Session-Id` to echo on subsequent requests, if the server
    issued one (full transport may also be stateless → id is None).
    """

    handshook: bool
    id: str | None


class HTTPMCPClient(MCPClient):
    """Streamable-HTTP MCP client. Stateless across calls — each public method
    runs a full session lifecycle over one keep-alive connection."""

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        auth_headers: dict[str, str] | None = None,
    ) -> None:
        self._url = url
        self._timeout_s = timeout_s
        # Static auth (Tier 1) — e.g. {"Authorization": "Bearer hf_…"}. Sent on
        # every request so authenticated servers (HF, API-key servers) work.
        self._auth_headers = auth_headers or {}

    # ------------------------------------------------------------------ public

    async def list_tools(self) -> list[MCPToolDef]:
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            session = await self._open_session(client)
            result = await self._request(client, session, "tools/list", {})
            await self._close_session(client, session)

        out: list[MCPToolDef] = []
        for t in result.get("tools", []) or []:
            if not isinstance(t, dict) or "name" not in t:
                continue
            out.append(
                MCPToolDef(
                    name=str(t["name"]),
                    description=str(t.get("description", "")),
                    input_schema=t.get("inputSchema") or {},
                )
            )
        return out

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPInvocationResult:
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            session = await self._open_session(client)
            result = await self._request(
                client, session, "tools/call", {"name": name, "arguments": arguments}
            )
            await self._close_session(client, session)

        text, images = _parse_content_blocks(result.get("content"))
        return MCPInvocationResult(
            content=text,
            is_error=bool(result.get("isError", False)),
            raw=result,
            images=images,
        )

    # ------------------------------------------------------- session lifecycle

    async def _open_session(self, client: httpx.AsyncClient) -> _Session:
        """Best-effort `initialize`. Returns a non-handshook session for servers
        that don't implement it (so we fall back to the minimal dialect)."""
        body = _envelope(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        try:
            resp = await client.post(
                self._url, json=body, headers={"Accept": _ACCEPT, **self._auth_headers}
            )
        except httpx.HTTPError:
            return _Session(handshook=False, id=None)
        if resp.status_code >= 400:
            return _Session(handshook=False, id=None)
        try:
            msg = _parse_message(resp, body["id"])
        except MCPError:
            return _Session(handshook=False, id=None)
        if "error" in msg:  # e.g. -32601 method not found → minimal server
            return _Session(handshook=False, id=None)

        session_id = resp.headers.get("mcp-session-id")
        # Spec: client SHOULD confirm readiness; some servers gate calls on it.
        await self._notify(client, "notifications/initialized", session_id)
        return _Session(handshook=True, id=session_id)

    async def _close_session(self, client: httpx.AsyncClient, session: _Session) -> None:
        """Free the server-side session (best-effort; not all servers support it)."""
        if not session.id:
            return
        with contextlib.suppress(httpx.HTTPError):
            await client.delete(self._url, headers=self._headers(session))

    async def _notify(self, client: httpx.AsyncClient, method: str, session_id: str | None) -> None:
        """Fire a JSON-RPC notification (no id, no result). Best-effort."""
        headers = {
            "Accept": _ACCEPT,
            "MCP-Protocol-Version": _PROTOCOL_VERSION,
            **self._auth_headers,
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        with contextlib.suppress(httpx.HTTPError):
            await client.post(self._url, json={"jsonrpc": "2.0", "method": method}, headers=headers)

    # --------------------------------------------------------------- request

    async def _request(
        self,
        client: httpx.AsyncClient,
        session: _Session,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        body = _envelope(method, params)
        try:
            resp = await client.post(self._url, json=body, headers=self._headers(session))
        except httpx.TimeoutException as e:
            raise MCPError(f"MCP server {self._url!r} timed out after {self._timeout_s}s") from e
        except httpx.HTTPError as e:
            raise MCPError(f"MCP transport error: {e}") from e

        if resp.status_code >= 400:
            # Body is often HTML/SSE — don't leak it into user-facing errors.
            raise MCPError(f"MCP server returned HTTP {resp.status_code}")

        msg = _parse_message(resp, body["id"])
        if "error" in msg:
            err = msg["error"]
            detail = err.get("message", err) if isinstance(err, dict) else err
            raise MCPError(f"MCP RPC error: {detail}")
        result = msg.get("result")
        if not isinstance(result, dict):
            raise MCPError(f"MCP result is not an object: {result!r}")
        return result

    def _headers(self, session: _Session) -> dict[str, str]:
        headers = {"Accept": _ACCEPT, **self._auth_headers}
        if session.handshook:
            headers["MCP-Protocol-Version"] = _PROTOCOL_VERSION
        if session.id:
            headers["Mcp-Session-Id"] = session.id
        return headers


# --------------------------------------------------------------------- helpers


def _envelope(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}


def _parse_content_blocks(content: Any) -> tuple[str, list[MCPImageBlock]]:
    """Split a tools/call `content` list into joined text + image blocks.

    MCP blocks are typed (`text`, `image`, `audio`, `resource`); we surface
    text and images and ignore the rest. Image blocks carry base64 `data` and
    a `mimeType`; malformed ones are skipped.
    """
    text_parts: list[str] = []
    images: list[MCPImageBlock] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        elif block.get("type") == "image":
            data = block.get("data")
            mime = block.get("mimeType") or block.get("mime_type")
            if isinstance(data, str) and isinstance(mime, str):
                images.append(MCPImageBlock(data=data, mime_type=mime))
    return "\n".join(text_parts), images


def _parse_message(resp: httpx.Response, expected_id: str) -> dict[str, Any]:
    """Return the JSON-RPC message answering `expected_id`.

    Handles a plain-JSON body and an SSE (`text/event-stream`) body — the latter
    carries one JSON-RPC message per `data:` event.
    """
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        messages = _parse_sse(resp.text)
    else:
        try:
            data = resp.json()
        except ValueError as e:
            raise MCPError(f"MCP response not JSON: {resp.text[:200]}") from e
        messages = data if isinstance(data, list) else [data]

    for m in messages:
        if isinstance(m, dict) and m.get("id") == expected_id:
            return m
    # Fall back to the first response-shaped message (some servers vary the id).
    for m in messages:
        if isinstance(m, dict) and ("result" in m or "error" in m):
            return m
    raise MCPError("MCP response contained no matching JSON-RPC message")


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """Extract JSON-RPC messages from an SSE stream.

    Events are blank-line separated; each carries its payload over one or more
    `data:` lines. Normalize CRLF/CR first — real servers (e.g. DeepWiki) use
    `\\r\\n`, and splitting on `\\n\\n` without normalizing would merge a
    multi-event stream into one block and corrupt the JSON.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    messages: list[dict[str, Any]] = []
    for block in normalized.split("\n\n"):
        data = "\n".join(
            line[len("data:") :].lstrip() for line in block.split("\n") if line.startswith("data:")
        )
        if not data:
            continue
        try:
            parsed = json.loads(data)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            messages.append(parsed)
    return messages
