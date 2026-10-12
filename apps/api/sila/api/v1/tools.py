"""Phase 5 — Tools & MCP endpoints.

Mounted at /v1/workspaces/{wid}/mcp-servers, /tools, and /agents/{aid}/tools.
All routes are workspace-scoped via the `require_workspace_role` dependency.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sila.adapters.auth.base import UserResult
from sila.api.deps import check_opa, require_workspace_role
from sila.core.config import settings
from sila.core.constants import Action, Role
from sila.db.repositories.agent import AgentRepository
from sila.db.repositories.mcp_server import MCPServerRepository, ToolRepository
from sila.db.session import get_db
from sila.services.tool_service import ToolService

router = APIRouter(tags=["tools"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegisterMCPServerRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: str = ""
    url: str
    # Tier-1 auth (optional). The token is encrypted at rest and never returned.
    auth_token: str | None = Field(default=None, max_length=4096)
    auth_header: str = Field(default="Authorization", max_length=64)
    auth_scheme: str = Field(default="Bearer", max_length=20)


class MCPServerOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    description: str
    url: str
    transport: str
    is_builtin: bool
    status: str
    # True when an auth token is stored — the token itself is never exposed.
    has_auth: bool = False

    @classmethod
    def from_model(cls, m: Any) -> MCPServerOut:
        return cls(
            id=m.id,
            workspace_id=m.workspace_id,
            name=m.name,
            description=m.description,
            url=m.url,
            transport=m.transport,
            is_builtin=m.is_builtin,
            status=m.status,
            has_auth=getattr(m, "auth_value_encrypted", None) is not None,
        )


class ToolOut(BaseModel):
    id: uuid.UUID
    mcp_server_id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    description: str
    input_schema: dict[str, Any]
    status: str

    @classmethod
    def from_model(cls, t: Any) -> ToolOut:
        return cls(
            id=t.id,
            mcp_server_id=t.mcp_server_id,
            workspace_id=t.workspace_id,
            name=t.name,
            description=t.description,
            input_schema=t.input_schema,
            status=t.status,
        )


class GrantToolRequest(BaseModel):
    tool_id: uuid.UUID


class InvokeToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolImageOut(BaseModel):
    # Ready-to-render data URL, e.g. "data:image/png;base64,iVBORw0K…".
    data_url: str


class ToolInvocationOut(BaseModel):
    id: uuid.UUID
    tool_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    outcome: str
    latency_ms: int
    output_preview: str
    error: str | None
    # Images returned by the tool (live response only; not persisted).
    images: list[ToolImageOut] = Field(default_factory=list)


def _service(db: AsyncSession) -> ToolService:
    return ToolService(db, builtin_hostnames=settings.mcp_builtin_hostname_set)


# ---------------------------------------------------------------------------
# MCP server endpoints (admin only)
# ---------------------------------------------------------------------------


@router.post(
    "/workspaces/{workspace_id}/mcp-servers",
    response_model=MCPServerOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_mcp_server(
    workspace_id: uuid.UUID,
    body: RegisterMCPServerRequest,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MCPServerOut:
    current_user, caller_role = caller
    if not await check_opa(Action.MCP_SERVER_REGISTER, caller_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    svc = _service(db)
    srv = await svc.register_mcp_server(
        workspace_id=workspace_id,
        actor_id=current_user.id,
        name=body.name,
        description=body.description,
        url=body.url,
        auth_token=body.auth_token,
        auth_header=body.auth_header,
        auth_scheme=body.auth_scheme,
    )
    return MCPServerOut.from_model(srv)


@router.get("/workspaces/{workspace_id}/mcp-servers", response_model=list[MCPServerOut])
async def list_mcp_servers(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[MCPServerOut]:
    repo = MCPServerRepository(db)
    servers = await repo.list_for_workspace(workspace_id)
    return [MCPServerOut.from_model(s) for s in servers]


@router.delete(
    "/workspaces/{workspace_id}/mcp-servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_mcp_server(
    workspace_id: uuid.UUID,
    server_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    current_user, caller_role = caller
    if not await check_opa(Action.MCP_SERVER_DELETE, caller_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    svc = _service(db)
    await svc.delete_mcp_server(
        server_id=server_id, workspace_id=workspace_id, actor_id=current_user.id
    )


@router.post(
    "/workspaces/{workspace_id}/mcp-servers/{server_id}/discover",
    response_model=list[ToolOut],
)
async def discover_tools(
    workspace_id: uuid.UUID,
    server_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.ADMIN))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ToolOut]:
    current_user, caller_role = caller
    if not await check_opa(Action.MCP_SERVER_DISCOVER, caller_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    svc = _service(db)
    tools = await svc.discover_tools(
        server_id=server_id, workspace_id=workspace_id, actor_id=current_user.id
    )
    return [ToolOut.from_model(t) for t in tools]


# ---------------------------------------------------------------------------
# Workspace tool catalog
# ---------------------------------------------------------------------------


@router.get("/workspaces/{workspace_id}/tools", response_model=list[ToolOut])
async def list_workspace_tools(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ToolOut]:
    repo = ToolRepository(db)
    tools = await repo.list_for_workspace(workspace_id)
    return [ToolOut.from_model(t) for t in tools]


# ---------------------------------------------------------------------------
# Per-agent whitelist
# ---------------------------------------------------------------------------


async def _ensure_agent_in_workspace(
    db: AsyncSession, workspace_id: uuid.UUID, agent_id: uuid.UUID
) -> None:
    # get() already filters by workspace_id; the post-fetch check is defence
    # in depth in case the row exists but is on a different workspace
    # (shouldn't happen — the index is unique).
    agent = await AgentRepository(db).get(agent_id, workspace_id)
    if not agent or agent.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")


@router.get("/workspaces/{workspace_id}/agents/{agent_id}/tools", response_model=list[ToolOut])
async def list_agent_tools(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ToolOut]:
    await _ensure_agent_in_workspace(db, workspace_id, agent_id)
    repo = ToolRepository(db)
    tools = await repo.list_for_agent(agent_id)
    return [ToolOut.from_model(t) for t in tools]


@router.post(
    "/workspaces/{workspace_id}/agents/{agent_id}/tools",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def grant_tool_to_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    body: GrantToolRequest,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    current_user, caller_role = caller
    if not await check_opa(Action.TOOL_GRANT, caller_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    await _ensure_agent_in_workspace(db, workspace_id, agent_id)

    svc = _service(db)
    await svc.grant_tool(
        agent_id=agent_id,
        tool_id=body.tool_id,
        workspace_id=workspace_id,
        actor_id=current_user.id,
    )


@router.delete(
    "/workspaces/{workspace_id}/agents/{agent_id}/tools/{tool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_tool_from_agent(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    current_user, caller_role = caller
    if not await check_opa(Action.TOOL_REVOKE, caller_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    await _ensure_agent_in_workspace(db, workspace_id, agent_id)

    svc = _service(db)
    await svc.revoke_tool(
        agent_id=agent_id,
        tool_id=tool_id,
        workspace_id=workspace_id,
        actor_id=current_user.id,
    )


@router.post(
    "/workspaces/{workspace_id}/agents/{agent_id}/tools/{tool_id}/invoke",
    response_model=ToolInvocationOut,
)
async def invoke_tool(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    body: InvokeToolRequest,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.BUILDER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolInvocationOut:
    current_user, caller_role = caller
    if not await check_opa(Action.TOOL_INVOKE, caller_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    await _ensure_agent_in_workspace(db, workspace_id, agent_id)

    svc = _service(db)
    res = await svc.invoke_tool(
        agent_id=agent_id,
        tool_id=tool_id,
        workspace_id=workspace_id,
        actor_id=current_user.id,
        arguments=body.arguments,
    )
    inv = res.invocation
    return ToolInvocationOut(
        id=inv.id,
        tool_id=inv.tool_id,
        agent_id=inv.agent_id,
        outcome=inv.outcome,
        latency_ms=inv.latency_ms,
        output_preview=inv.output_preview,
        error=inv.error,
        images=[ToolImageOut(data_url=u) for u in res.image_urls],
    )


@router.get("/workspaces/{workspace_id}/tool-invocations", response_model=list[ToolInvocationOut])
async def list_recent_invocations(
    workspace_id: uuid.UUID,
    caller: Annotated[tuple[UserResult, Role], Depends(require_workspace_role(Role.VIEWER))],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 50,
) -> list[ToolInvocationOut]:
    from sqlalchemy import select

    from sila.db.models import ToolInvocation

    limit = min(max(limit, 1), 200)
    result = await db.execute(
        select(ToolInvocation)
        .where(ToolInvocation.workspace_id == workspace_id)
        .order_by(ToolInvocation.created_at.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    return [
        ToolInvocationOut(
            id=r.id,
            tool_id=r.tool_id,
            agent_id=r.agent_id,
            outcome=r.outcome,
            latency_ms=r.latency_ms,
            output_preview=r.output_preview,
            error=r.error,
        )
        for r in rows
    ]
