"""Orchestrates the MCP/Tool lifecycle: register, discover, grant, invoke.

Every state-changing method:
  - validates inputs at the boundary (URL via SSRF guard; agent ownership)
  - writes an audit_log entry
  - returns the ORM model

Invocation hot path:
  - validates the agent has the tool whitelisted (per-agent ACL)
  - validates input against the tool's JSON Schema (jsonschema)
  - calls the MCP server via the HTTPMCPClient adapter
  - records a tool_invocation row with hashes + previews + latency + outcome
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, status
from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.mcp.base import MCPClient, MCPImageBlock
from sila.adapters.mcp.http_client import HTTPMCPClient, MCPError
from sila.core.constants import Action, Outcome, ResourceType
from sila.core.security.field_crypto import FieldDecryptionError, decrypt_field, encrypt_field
from sila.core.security.ssrf_guard import validate_external_url
from sila.db.models import MCPServer, Tool, ToolInvocation
from sila.db.repositories.audit import AuditRepository
from sila.db.repositories.mcp_server import MCPServerRepository, ToolRepository

logger = logging.getLogger(__name__)

PREVIEW_MAX_CHARS = 200


# Image URLs embedded in a tool's text output. Gradio-backed MCP tools (e.g.
# Z-Image) return a file URL rather than a base64 image block, so we surface
# those too. Restricted to image extensions + http(s).
_IMAGE_URL_RE = re.compile(r"https?://[^\s'\"<>)]+\.(?:png|jpe?g|webp|gif|bmp|svg)", re.IGNORECASE)


@dataclass
class InvokeResult:
    """A completed tool invocation plus any renderable image URLs it returned.

    `image_urls` are ready-to-render `<img src>` strings — either a base64
    `data:` URL (from an MCP image block) or an external https URL found in the
    text output. Returned to the caller (playground) but never persisted; the
    DB row keeps only a short text preview.
    """

    invocation: ToolInvocation
    image_urls: list[str] = field(default_factory=list)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _preview(s: str) -> str:
    return s[:PREVIEW_MAX_CHARS]


def _extract_image_urls(text: str) -> list[str]:
    """Find image URLs in tool text output, de-duplicated, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for url in _IMAGE_URL_RE.findall(text or ""):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


class ToolService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        # Callable rather than type[MCPClient] — Protocol classes can't be
        # instantiated, so mypy treats type[Protocol] as having no constructor.
        # "give me a URL (+ optional auth headers), I'll give you a client."
        mcp_client_factory: Callable[..., MCPClient] = HTTPMCPClient,
        builtin_hostnames: frozenset[str] | None = None,
    ) -> None:
        self._db = db
        self._mcp_servers = MCPServerRepository(db)
        self._tools = ToolRepository(db)
        self._audit = AuditRepository(db)
        self._client_factory = mcp_client_factory
        # Hostnames that bypass SSRF check (Docker-network built-in sidecars).
        # Built-ins are flagged at registration time; runtime invocation re-validates.
        self._builtin_hostnames = builtin_hostnames or frozenset()

    def _auth_headers_for(self, srv: MCPServer) -> dict[str, str] | None:
        """Decrypt the server's stored token into a request header, if any.

        Returns e.g. {"Authorization": "Bearer hf_…"}. A decryption failure
        (key rotated) degrades to no-auth rather than crashing the call.
        """
        if not srv.auth_value_encrypted:
            return None
        try:
            token = decrypt_field(srv.auth_value_encrypted)
        except FieldDecryptionError:
            logger.warning("MCP server %s auth token could not be decrypted", srv.id)
            return None
        value = f"{srv.auth_scheme} {token}".strip() if srv.auth_scheme else token
        return {srv.auth_header: value}

    # ---------- MCP server lifecycle ----------

    async def register_mcp_server(
        self,
        *,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        name: str,
        description: str,
        url: str,
        auth_token: str | None = None,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
    ) -> MCPServer:
        """Validates URL via SSRF guard, then stores the server. Admin role required at API layer.

        `is_builtin` is auto-detected: if the URL's hostname is in the configured
        `mcp_builtin_hostnames` allowlist (Docker-network sidecars), the row is
        flagged builtin and the SSRF check permits its private IP.
        """
        if await self._mcp_servers.name_exists(workspace_id, name):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An MCP server named '{name}' already exists in this workspace.",
            )

        try:
            validated_url = await validate_external_url(
                url, allow_hostnames=self._builtin_hostnames
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

        is_builtin = urlsplit(validated_url).hostname in self._builtin_hostnames

        token = (auth_token or "").strip()
        encrypted = encrypt_field(token) if token else None

        async with self._db.begin_nested():
            srv = await self._mcp_servers.create(
                workspace_id=workspace_id,
                name=name,
                description=description,
                url=validated_url,
                registered_by=actor_id,
                is_builtin=is_builtin,
                auth_value_encrypted=encrypted,
                auth_header=(auth_header or "Authorization").strip() or "Authorization",
                auth_scheme=(auth_scheme or "").strip(),
            )
            await self._audit.record(
                action=Action.MCP_SERVER_REGISTER,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.MCP_SERVER,
                resource_id=srv.id,
            )
        from sila.services.sila.mcp_tools import invalidate as _invalidate_mcp_cache

        _invalidate_mcp_cache(workspace_id)
        return srv

    async def delete_mcp_server(
        self, *, server_id: uuid.UUID, workspace_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        srv = await self._mcp_servers.get(server_id)
        if not srv or srv.workspace_id != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found"
            )

        async with self._db.begin_nested():
            await self._audit.record(
                action=Action.MCP_SERVER_DELETE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.MCP_SERVER,
                resource_id=server_id,
            )
            await self._mcp_servers.delete(srv)
        from sila.services.sila.mcp_tools import invalidate as _invalidate_mcp_cache

        _invalidate_mcp_cache(workspace_id)

    async def discover_tools(
        self, *, server_id: uuid.UUID, workspace_id: uuid.UUID, actor_id: uuid.UUID
    ) -> list[Tool]:
        """Call the MCP server's tools/list, upsert each tool, deactivate any that disappeared."""
        srv = await self._mcp_servers.get(server_id)
        if not srv or srv.workspace_id != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found"
            )

        # Re-validate URL at discovery time (DNS rebinding mitigation; built-ins bypass)
        try:
            await validate_external_url(
                srv.url,
                allow_hostnames=self._builtin_hostnames if srv.is_builtin else None,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"MCP server URL is no longer safe to call: {e}",
            ) from e

        client = self._client_factory(srv.url, auth_headers=self._auth_headers_for(srv))
        try:
            defs = await client.list_tools()
        except MCPError as e:
            await self._audit.record(
                action=Action.MCP_SERVER_DISCOVER,
                outcome=Outcome.FAILURE,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.MCP_SERVER,
                resource_id=server_id,
                metadata={"error": str(e)},
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"MCP discovery failed: {e}",
            ) from e

        async with self._db.begin_nested():
            present: set[str] = set()
            for d in defs:
                await self._tools.upsert_for_server(
                    mcp_server_id=server_id,
                    workspace_id=workspace_id,
                    name=d.name,
                    description=d.description,
                    input_schema=d.input_schema,
                )
                present.add(d.name)
            await self._tools.deactivate_missing(server_id, present)
            await self._audit.record(
                action=Action.MCP_SERVER_DISCOVER,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.MCP_SERVER,
                resource_id=server_id,
                metadata={"tool_count": len(defs)},
            )

        return await self._tools.list_for_server(server_id)

    # ---------- Per-agent whitelist ----------

    async def grant_tool(
        self,
        *,
        agent_id: uuid.UUID,
        tool_id: uuid.UUID,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> None:
        tool = await self._tools.get(tool_id)
        if not tool or tool.workspace_id != workspace_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")

        if await self._tools.is_granted(agent_id, tool_id):
            return  # idempotent

        async with self._db.begin_nested():
            await self._tools.grant_to_agent(
                agent_id=agent_id,
                tool_id=tool_id,
                workspace_id=workspace_id,
                granted_by=actor_id,
            )
            await self._audit.record(
                action=Action.TOOL_GRANT,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.TOOL,
                resource_id=tool_id,
                metadata={"agent_id": str(agent_id)},
            )

    async def revoke_tool(
        self,
        *,
        agent_id: uuid.UUID,
        tool_id: uuid.UUID,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> None:
        async with self._db.begin_nested():
            removed = await self._tools.revoke_from_agent(agent_id, tool_id)
            if not removed:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Tool not granted to this agent"
                )
            await self._audit.record(
                action=Action.TOOL_REVOKE,
                outcome=Outcome.SUCCESS,
                actor_user_id=actor_id,
                actor_workspace_id=workspace_id,
                resource_type=ResourceType.TOOL,
                resource_id=tool_id,
                metadata={"agent_id": str(agent_id)},
            )

    # ---------- Invocation ----------

    async def invoke_tool(
        self,
        *,
        agent_id: uuid.UUID,
        tool_id: uuid.UUID,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        arguments: dict[str, Any],
    ) -> InvokeResult:
        """Validate whitelist + schema, call MCP, record invocation.
        O(1) writes + 1 network call."""
        tool = await self._tools.get(tool_id)
        if not tool or tool.workspace_id != workspace_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
        if tool.status != "active":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tool is disabled")

        if not await self._tools.is_granted(agent_id, tool_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This tool is not granted to this agent",
            )

        # Validate arguments against the cached JSON Schema.
        try:
            if tool.input_schema:
                Draft202012Validator(tool.input_schema).validate(arguments)
        except ValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Tool input validation failed: {e.message}",
            ) from e

        srv = await self._mcp_servers.get(tool.mcp_server_id)
        if not srv:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Tool's MCP server no longer exists",
            )

        # Re-resolve URL on every invocation to mitigate DNS rebinding.
        try:
            await validate_external_url(
                srv.url,
                allow_hostnames=self._builtin_hostnames if srv.is_builtin else None,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"MCP URL no longer safe to call: {e}",
            ) from e

        input_str = json.dumps(arguments, sort_keys=True, default=str)
        input_hash = _sha256(input_str)
        input_preview = _preview(input_str)

        start = time.perf_counter()
        auth_headers = self._auth_headers_for(srv)
        client = self._client_factory(srv.url, auth_headers=auth_headers)
        outcome = Outcome.SUCCESS
        err_msg: str | None = None
        output_content = ""
        output_images: list[MCPImageBlock] = []
        try:
            result = await client.call_tool(tool.name, arguments)
            output_content = result.content
            output_images = result.images
            if result.is_error:
                outcome = Outcome.FAILURE
                err_msg = output_content[:500]
        except MCPError as e:
            outcome = Outcome.FAILURE
            err_msg = str(e)
        latency_ms = int((time.perf_counter() - start) * 1000)

        # Keep the DB preview text-only; note images instead of storing base64.
        preview_text = output_content
        if not preview_text and output_images:
            plural = "s" if len(output_images) != 1 else ""
            preview_text = f"[{len(output_images)} image{plural} returned]"

        invocation = ToolInvocation(
            workspace_id=workspace_id,
            agent_id=agent_id,
            tool_id=tool_id,
            caller_user_id=actor_id,
            input_hash=input_hash,
            input_preview=input_preview,
            output_hash=_sha256(output_content) if output_content else None,
            output_preview=_preview(preview_text),
            latency_ms=latency_ms,
            outcome=outcome.value,
            error=err_msg,
        )
        self._db.add(invocation)
        await self._db.flush()

        await self._audit.record(
            action=Action.TOOL_INVOKE,
            outcome=outcome,
            actor_user_id=actor_id,
            actor_workspace_id=workspace_id,
            resource_type=ResourceType.TOOL,
            resource_id=tool_id,
            metadata={"agent_id": str(agent_id), "latency_ms": latency_ms},
        )

        if outcome == Outcome.FAILURE and err_msg:
            # Surface to caller but keep the audit row.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Tool invocation failed: {err_msg}",
            )

        # Renderable images: base64 blocks → data URLs directly, plus any image
        # URLs in the (full, untruncated) text output. Those URLs are often
        # auth-gated (Gradio/HF file endpoints) so the browser can't load them —
        # fetch them server-side with the server's token and inline as base64.
        image_urls = [f"data:{im.mime_type};base64,{im.data}" for im in output_images]
        extracted = _extract_image_urls(output_content)
        image_urls.extend(await self._inline_images(extracted, auth_headers))
        return InvokeResult(invocation=invocation, image_urls=image_urls)

    async def _inline_images(
        self, urls: list[str], auth_headers: dict[str, str] | None
    ) -> list[str]:
        """Fetch image URLs server-side (SSRF-guarded, with the server's auth)
        and return them as base64 data URLs the browser can render without auth.

        The token is the one the admin gave this (trusted) MCP server, sent only
        to URLs that server itself returned. SSRF guard blocks internal targets.
        Failures are skipped silently — the text output still carries the URL.
        """
        out: list[str] = []
        for url in urls[:4]:  # cap: a tool shouldn't flood the response
            try:
                await validate_external_url(url)
            except ValueError:
                continue
            try:
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
                    resp = await http.get(url, headers=auth_headers or {})
            except httpx.HTTPError:
                logger.warning("Could not fetch tool image %s", url)
                continue
            ctype = resp.headers.get("content-type", "").split(";")[0].strip()
            if resp.status_code != 200 or not ctype.startswith("image/"):
                continue
            if len(resp.content) > 8 * 1024 * 1024:  # 8 MB cap
                continue
            out.append(f"data:{ctype};base64,{base64.b64encode(resp.content).decode()}")
        return out
