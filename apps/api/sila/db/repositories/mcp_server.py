"""Data access for MCP servers and their discovered tools.

All methods are workspace-scoped — callers must pass workspace_id to enforce
tenant isolation alongside RLS.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import AgentTool, MCPServer, Tool


class MCPServerRepository:
    """O(log n) queries via indexes on (workspace_id, status)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        description: str,
        url: str,
        registered_by: uuid.UUID,
        is_builtin: bool = False,
        auth_value_encrypted: bytes | None = None,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
    ) -> MCPServer:
        srv = MCPServer(
            workspace_id=workspace_id,
            name=name,
            description=description,
            url=url,
            registered_by=registered_by,
            is_builtin=is_builtin,
            auth_value_encrypted=auth_value_encrypted,
            auth_header=auth_header,
            auth_scheme=auth_scheme,
        )
        self._db.add(srv)
        await self._db.flush()
        return srv

    async def get(self, server_id: uuid.UUID) -> MCPServer | None:
        return await self._db.get(MCPServer, server_id)

    async def list_for_workspace(self, workspace_id: uuid.UUID) -> list[MCPServer]:
        result = await self._db.execute(
            select(MCPServer)
            .where(MCPServer.workspace_id == workspace_id)
            .order_by(MCPServer.created_at.desc())
        )
        return list(result.scalars().all())

    async def name_exists(self, workspace_id: uuid.UUID, name: str) -> bool:
        result = await self._db.execute(
            select(MCPServer.id)
            .where(MCPServer.workspace_id == workspace_id, MCPServer.name == name)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def delete(self, srv: MCPServer) -> None:
        await self._db.delete(srv)
        await self._db.flush()


class ToolRepository:
    """O(log n) tool queries; agent_tools join handles whitelist lookups."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def upsert_for_server(
        self,
        *,
        mcp_server_id: uuid.UUID,
        workspace_id: uuid.UUID,
        name: str,
        description: str,
        input_schema: dict[str, Any],
    ) -> Tool:
        """Upsert by (mcp_server_id, name). Used by discovery refresh."""
        result = await self._db.execute(
            select(Tool).where(Tool.mcp_server_id == mcp_server_id, Tool.name == name)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.description = description
            existing.input_schema = input_schema
            existing.status = "active"
            await self._db.flush()
            return existing
        t = Tool(
            mcp_server_id=mcp_server_id,
            workspace_id=workspace_id,
            name=name,
            description=description,
            input_schema=input_schema,
        )
        self._db.add(t)
        await self._db.flush()
        return t

    async def deactivate_missing(self, mcp_server_id: uuid.UUID, present_names: set[str]) -> None:
        """Tools no longer returned by discovery are marked disabled (not deleted —
        agents may still reference them; the audit trail stays intact)."""
        result = await self._db.execute(
            select(Tool).where(Tool.mcp_server_id == mcp_server_id, Tool.status == "active")
        )
        for t in result.scalars().all():
            if t.name not in present_names:
                t.status = "disabled"

    async def get(self, tool_id: uuid.UUID) -> Tool | None:
        return await self._db.get(Tool, tool_id)

    async def list_for_workspace(self, workspace_id: uuid.UUID) -> list[Tool]:
        result = await self._db.execute(
            select(Tool)
            .where(Tool.workspace_id == workspace_id, Tool.status == "active")
            .order_by(Tool.name.asc())
        )
        return list(result.scalars().all())

    async def list_for_server(self, mcp_server_id: uuid.UUID) -> list[Tool]:
        result = await self._db.execute(
            select(Tool).where(Tool.mcp_server_id == mcp_server_id).order_by(Tool.name.asc())
        )
        return list(result.scalars().all())

    async def list_for_agent(self, agent_id: uuid.UUID) -> list[Tool]:
        """Tools whitelisted to a specific agent. O(k) where k = grants for agent."""
        result = await self._db.execute(
            select(Tool)
            .join(AgentTool, AgentTool.tool_id == Tool.id)
            .where(AgentTool.agent_id == agent_id)
            .order_by(Tool.name.asc())
        )
        return list(result.scalars().all())

    async def is_granted(self, agent_id: uuid.UUID, tool_id: uuid.UUID) -> bool:
        result = await self._db.execute(
            select(AgentTool.tool_id)
            .where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def grant_to_agent(
        self,
        *,
        agent_id: uuid.UUID,
        tool_id: uuid.UUID,
        workspace_id: uuid.UUID,
        granted_by: uuid.UUID,
    ) -> AgentTool:
        link = AgentTool(
            agent_id=agent_id,
            tool_id=tool_id,
            workspace_id=workspace_id,
            granted_by=granted_by,
        )
        self._db.add(link)
        await self._db.flush()
        return link

    async def revoke_from_agent(self, agent_id: uuid.UUID, tool_id: uuid.UUID) -> bool:
        result = await self._db.execute(
            select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
        )
        link = result.scalar_one_or_none()
        if not link:
            return False
        await self._db.delete(link)
        await self._db.flush()
        return True
