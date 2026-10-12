"""Expose a workspace's registered MCP tools to SILA's function-calling brain.

Phase 5 shipped MCP registration/discovery/invocation and the per-agent tool
playground — but SILA's conversational loop ran on a FIXED curated tool set, so
it could register an MCP server yet never *use* its tools mid-conversation. This
module closes that gap: it lists the active servers' tools, exposes them as
function-calling schemas (namespaced so they can't collide with the curated
registry), and routes a call back through the same HTTPMCPClient + encrypted
auth the playground uses. Role-gated + audited by the caller (execute_tool).

Namespacing: `mcp__<server-slug>__<tool>` — the double underscore is the split
token; the server slug maps back to the exact `mcp_servers.id` via an in-turn
lookup, so a renamed/duplicate server can't be spoofed by name alone.

Caching: `list_tools` is a network round-trip, so the per-workspace schema list
is cached with a short TTL — one discovery serves many conversation turns.

Complexity: schema build O(servers × tools); a call is O(1) lookup + one round-trip.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.mcp.base import MCPClient
from sila.adapters.mcp.http_client import HTTPMCPClient, MCPError
from sila.core.security.field_crypto import FieldDecryptionError, decrypt_field
from sila.db.models import MCPServer
from sila.db.repositories.mcp_server import MCPServerRepository

log = structlog.get_logger(__name__)

_NS = "mcp__"  # namespace prefix + the split token that follows the server slug
_TTL_S = 120.0  # discovery cache TTL — one list_tools serves ~2 min of turns
_MAX_TOOLS = 40  # cap the tools offered to the model (keeps the prompt bounded)

# Stale-while-revalidate. Discovery costs ~1.25 s (measured 2026-08-16: six
# registered servers, several of them public internet endpoints), and paying it
# inside a turn put it on the critical path of the first request after every TTL
# expiry. Past _REFRESH_AFTER_S we serve the CACHED value immediately and refresh
# in the background, so a turn only ever blocks on discovery when there is no
# cached entry at all (cold process) or the entry is older than _HARD_TTL_S.
_REFRESH_AFTER_S = 90.0
_HARD_TTL_S = 900.0
_refreshing: set[uuid.UUID] = set()


def _slug(name: str) -> str:
    """A server name → a stable, collision-safe slug for the tool namespace."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "server"


@dataclass
class _CacheEntry:
    at: float
    schemas: list[dict[str, Any]]
    # slug → (server_id, real tool name) so a call routes to the exact server.
    routes: dict[str, tuple[uuid.UUID, str]]


_cache: dict[uuid.UUID, _CacheEntry] = {}


def _auth_headers_for(srv: MCPServer) -> dict[str, str] | None:
    """Decrypt the server's static token into a request header, or None. A
    decryption failure fails closed (no header) — the same rule as tool_service."""
    if not srv.auth_value_encrypted:
        return None
    try:
        token = decrypt_field(srv.auth_value_encrypted)
    except FieldDecryptionError:
        log.warning("sila.mcp.auth_decrypt_failed", server_id=str(srv.id))
        return None
    header = srv.auth_header or "Authorization"
    scheme = srv.auth_scheme or "Bearer"
    return {header: f"{scheme} {token}".strip()}


def _client_for(srv: MCPServer) -> MCPClient:
    return HTTPMCPClient(srv.url, auth_headers=_auth_headers_for(srv))


async def workspace_mcp_schemas(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    client_factory: Any = None,
) -> list[dict[str, Any]]:
    """Function-calling schemas for every tool the workspace's ACTIVE MCP servers
    expose, namespaced `mcp__<slug>__<tool>`. Cached per workspace (TTL). A server
    that errors on discovery is skipped (best-effort — one bad server never blocks
    the others or the turn). The route map is cached alongside for the call side."""
    factory = client_factory or _client_for
    cached = _cache.get(workspace_id)
    if cached:
        age = time.monotonic() - cached.at
        if age < _REFRESH_AFTER_S:
            return cached.schemas
        if age < _HARD_TTL_S:
            # Serve stale NOW, refresh off the critical path so no turn waits.
            _schedule_refresh(workspace_id, client_factory)
            return cached.schemas

    servers = await MCPServerRepository(db).list_for_workspace(workspace_id)
    schemas: list[dict[str, Any]] = []
    routes: dict[str, tuple[uuid.UUID, str]] = {}
    for srv in servers:
        if getattr(srv, "status", "active") != "active":
            continue
        slug = _slug(srv.name)
        try:
            defs = await factory(srv).list_tools()
        except (MCPError, Exception) as exc:  # noqa: BLE001 — one server never blocks the rest
            log.warning("sila.mcp.discovery_failed", server=srv.name, error=type(exc).__name__)
            continue
        for tool in defs:
            if len(schemas) >= _MAX_TOOLS:
                break
            namespaced = f"{_NS}{slug}__{tool.name}"
            routes[namespaced] = (srv.id, tool.name)
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": namespaced,
                        "description": f"[{srv.name}] {tool.description}"[:1024],
                        "parameters": tool.input_schema or {"type": "object", "properties": {}},
                    },
                }
            )

    _cache[workspace_id] = _CacheEntry(at=time.monotonic(), schemas=schemas, routes=routes)
    return schemas


def _schedule_refresh(workspace_id: uuid.UUID, client_factory: Any = None) -> None:
    """Kick off a background re-discovery for `workspace_id` (at most one in
    flight per workspace). Uses its OWN DB session: the caller's session belongs
    to the request and may close before this finishes. Failures are logged and
    leave the stale entry in place — a refresh must never break a turn."""
    if workspace_id in _refreshing:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # no loop (sync context) — skip; the next call refreshes
        return
    _refreshing.add(workspace_id)

    async def _run() -> None:
        try:
            from sila.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as fresh_db:
                # Force a real discovery by clearing this workspace's entry only
                # after we have the new one, so readers never see an empty cache.
                prev = _cache.pop(workspace_id, None)
                try:
                    await workspace_mcp_schemas(
                        fresh_db, workspace_id, client_factory=client_factory
                    )
                except Exception:
                    if prev is not None:  # restore the stale entry on failure
                        _cache[workspace_id] = prev
                    raise
        except Exception as exc:  # noqa: BLE001 — background refresh is best-effort
            log.warning("sila.mcp.background_refresh_failed", error=type(exc).__name__)
        finally:
            _refreshing.discard(workspace_id)

    loop.create_task(_run())


def is_mcp_tool(name: str) -> bool:
    return name.startswith(_NS)


async def call_mcp_tool(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    name: str,
    args: dict[str, Any],
    *,
    client_factory: Any = None,
) -> str:
    """Route a namespaced MCP tool call to its server. The route comes from the
    cached discovery (rebuilt if the cache expired), so an arbitrary
    `mcp__…`-looking name that isn't a real registered tool is refused — never
    trusted from the model. Returns the tool's text output (or a clear error)."""
    factory = client_factory or _client_for
    entry = _cache.get(workspace_id)
    if entry is None or (time.monotonic() - entry.at) >= _TTL_S:
        await workspace_mcp_schemas(db, workspace_id, client_factory=factory)
        entry = _cache.get(workspace_id)
    route = entry.routes.get(name) if entry else None
    if route is None:
        return f"'{name}' isn't a currently available tool. Ask me to reconnect it, or skip it."
    server_id, real_name = route
    srv = await MCPServerRepository(db).get(server_id)
    if srv is None:
        return "That tool's server is no longer connected."
    try:
        result = await factory(srv).call_tool(real_name, args or {})
    except MCPError as exc:
        log.warning("sila.mcp.call_failed", server=srv.name, tool=real_name, error=str(exc))
        return f"The tool call failed: {exc}"
    if result.is_error:
        return f"The tool reported an error: {result.content[:500]}"
    return result.content[:4000] or "(the tool returned no text)"


def invalidate(workspace_id: uuid.UUID) -> None:
    """Drop the cached discovery (call after registering/removing a server)."""
    _cache.pop(workspace_id, None)
