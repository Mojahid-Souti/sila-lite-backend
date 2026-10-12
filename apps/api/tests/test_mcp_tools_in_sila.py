"""SILA can call a workspace's registered MCP tools mid-conversation.

Phase 5 shipped MCP registration/discovery/invocation; this closes the gap where
SILA's brain couldn't actually USE those tools. No DB/network — a fake MCP client
and a fake server repo drive the seam.
"""

from __future__ import annotations

import uuid

import pytest

from sila.adapters.mcp.base import MCPInvocationResult, MCPToolDef
from sila.services.sila import mcp_tools


class _FakeSrv:
    def __init__(self, name: str, status: str = "active") -> None:
        self.id = uuid.uuid4()
        self.name = name
        self.url = f"http://{name}"
        self.status = status
        self.auth_value_encrypted = None
        self.auth_header = "Authorization"
        self.auth_scheme = "Bearer"


class _FakeClient:
    def __init__(self, srv: _FakeSrv, *, tools: list[MCPToolDef], result: str = "ok") -> None:
        self._tools = tools
        self._result = result
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self) -> list[MCPToolDef]:
        return self._tools

    async def call_tool(self, name: str, arguments: dict) -> MCPInvocationResult:
        self.calls.append((name, arguments))
        return MCPInvocationResult(content=self._result, is_error=False, raw={})


class _FakeRepo:
    """Stands in for MCPServerRepository — patched onto the module."""

    _servers: list[_FakeSrv] = []

    def __init__(self, _db: object) -> None: ...

    async def list_for_workspace(self, _ws: uuid.UUID) -> list[_FakeSrv]:
        return _FakeRepo._servers

    async def get(self, server_id: uuid.UUID) -> _FakeSrv | None:
        return next((s for s in _FakeRepo._servers if s.id == server_id), None)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):  # type: ignore[no-untyped-def]
    mcp_tools._cache.clear()
    monkeypatch.setattr(mcp_tools, "MCPServerRepository", _FakeRepo)
    _FakeRepo._servers = []
    yield
    mcp_tools._cache.clear()


async def test_schemas_are_namespaced_and_scoped() -> None:
    srv = _FakeSrv("Hugging Face")
    _FakeRepo._servers = [srv]
    client = _FakeClient(srv, tools=[MCPToolDef("search", "Search models", {"type": "object"})])
    schemas = await mcp_tools.workspace_mcp_schemas(
        None, uuid.uuid4(), client_factory=lambda _s: client
    )
    assert len(schemas) == 1
    fn = schemas[0]["function"]
    assert fn["name"] == "mcp__hugging-face__search"
    assert fn["description"].startswith("[Hugging Face] Search models")
    assert mcp_tools.is_mcp_tool(fn["name"]) and not mcp_tools.is_mcp_tool("navigate")


async def test_call_routes_to_the_right_server() -> None:
    srv = _FakeSrv("Time")
    _FakeRepo._servers = [srv]
    client = _FakeClient(srv, tools=[MCPToolDef("now", "Current time", {})], result="12:00")
    ws = uuid.uuid4()
    await mcp_tools.workspace_mcp_schemas(None, ws, client_factory=lambda _s: client)
    out = await mcp_tools.call_mcp_tool(
        None, ws, "mcp__time__now", {"tz": "UTC"}, client_factory=lambda _s: client
    )
    assert out == "12:00"
    assert client.calls == [("now", {"tz": "UTC"})]


async def test_unknown_mcp_name_is_refused_not_called() -> None:
    _FakeRepo._servers = [_FakeSrv("Time")]
    ws = uuid.uuid4()
    out = await mcp_tools.call_mcp_tool(
        None, ws, "mcp__evil__exfiltrate", {}, client_factory=lambda s: _FakeClient(s, tools=[])
    )
    assert "isn't a currently available tool" in out


async def test_discovery_skips_a_failing_server() -> None:
    good, bad = _FakeSrv("Good"), _FakeSrv("Bad")
    _FakeRepo._servers = [bad, good]

    class _BadClient(_FakeClient):
        async def list_tools(self):  # type: ignore[override]
            raise RuntimeError("server down")

    def factory(s: _FakeSrv):
        return (
            _BadClient(s, tools=[])
            if s.name == "Bad"
            else _FakeClient(s, tools=[MCPToolDef("ping", "Ping", {})])
        )

    schemas = await mcp_tools.workspace_mcp_schemas(None, uuid.uuid4(), client_factory=factory)
    names = [s["function"]["name"] for s in schemas]
    assert names == ["mcp__good__ping"]  # the bad server didn't block the good one


async def test_inactive_server_is_excluded() -> None:
    _FakeRepo._servers = [_FakeSrv("Paused", status="disabled")]
    schemas = await mcp_tools.workspace_mcp_schemas(
        None, uuid.uuid4(), client_factory=lambda s: _FakeClient(s, tools=[MCPToolDef("x", "", {})])
    )
    assert schemas == []


async def test_execute_tool_routes_mcp_and_role_gates(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext, execute_tool

    srv = _FakeSrv("Time")
    _FakeRepo._servers = [srv]
    client = _FakeClient(srv, tools=[MCPToolDef("now", "time", {})], result="noon")
    ws = uuid.uuid4()
    await mcp_tools.workspace_mcp_schemas(None, ws, client_factory=lambda _s: client)
    monkeypatch.setattr(mcp_tools, "_client_for", lambda _s: client)

    def _ctx(role: Role) -> ToolContext:
        return ToolContext(
            db=None,  # type: ignore[arg-type]
            user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
            workspace_id=ws,
            role=role,
        )

    ok = await execute_tool("mcp__time__now", _ctx(Role.BUILDER), {})
    assert ok.content == "noon"
    # A Viewer can't call external tools (matches the tool-playground gate).
    denied = await execute_tool("mcp__time__now", _ctx(Role.VIEWER), {})
    assert "doesn't permit external tools" in denied.content


def test_tool_schemas_appends_extra() -> None:
    from sila.services.sila.tool_registry import tool_schemas

    extra = [
        {
            "type": "function",
            "function": {"name": "mcp__x__y", "description": "d", "parameters": {}},
        }
    ]
    names = [s["function"]["name"] for s in tool_schemas(extra=extra)]
    assert "mcp__x__y" in names and "navigate" in names
