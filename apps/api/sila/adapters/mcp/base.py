"""MCP client adapter interface.

Rule 5 (production-ready): all MCP communication goes through this Protocol so
new transports (stdio, websocket) can be added without touching the service layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class MCPToolDef:
    """A tool returned by an MCP server's tools/list call."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class MCPImageBlock:
    """An image content block from a tools/call result (base64-encoded)."""

    data: str
    mime_type: str


@dataclass
class MCPInvocationResult:
    """Result returned from an MCP server's tools/call invocation.

    `content` is the concatenated text. `images` holds any image blocks so
    callers can surface them (e.g. an image-generation tool) without parsing
    `raw` themselves.
    """

    content: str
    is_error: bool
    raw: dict[str, Any]
    images: list[MCPImageBlock] = field(default_factory=list)


class MCPClient(Protocol):
    """Protocol for an MCP client. Implementations: HTTP (Phase 5), stdio (later)."""

    async def list_tools(self) -> list[MCPToolDef]:
        """Discover tools exposed by the server. O(1) network call."""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPInvocationResult:
        """Invoke a tool by name. Caller has already validated `arguments`."""
        ...
