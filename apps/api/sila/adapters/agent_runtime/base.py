"""AgentRuntime interface — production-ready Protocol (Rule 5).

DifyAdapter implements this now. Future adapters (LangGraph, custom) swap in via config.
"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable


class AgentDefinitionError(Exception):
    """The agent's DSL can't be registered because the definition itself is
    invalid (vs the runtime being down). Maps to a 4xx, not a 503."""


@runtime_checkable
class AgentRuntime(Protocol):
    async def register_app(self, name: str, dsl: dict) -> str:  # type: ignore[type-arg]
        """Register a new Dify app from DSL. Returns the Dify app_id (str)."""
        ...

    async def update_app(self, app_id: str, dsl: dict) -> None:  # type: ignore[type-arg]
        """Push updated DSL to an existing Dify app."""
        ...

    async def invoke_sandbox(self, app_id: str, query: str, user_id: str) -> dict:  # type: ignore[type-arg]
        """Run a non-streaming sandbox invocation. Returns {"answer": str, "usage": {...}}."""
        ...

    def stream_sandbox(self, app_id: str, query: str, user_id: str) -> AsyncIterator[dict]:  # type: ignore[type-arg]
        """Streaming sandbox invocation. Async-yields ``{"token": str}`` chunks as
        they arrive, then a final ``{"usage": {...}}``. (Declared non-async because
        the implementation is an async generator — calling it returns the iterator.)
        """
        ...

    async def validate_dsl(self, dsl: dict) -> list[str]:  # type: ignore[type-arg]
        """Validate DSL structure. Returns list of human-readable errors (empty = valid)."""
        ...

    async def delete_app(self, app_id: str) -> None:
        """Delete a Dify app by its app_id."""
        ...

    async def list_apps(self) -> list[dict]:  # type: ignore[type-arg]
        """List apps in the connected runtime (Build-in-Dify round-trip).
        Returns ``[{id, name, mode, description}]``."""
        ...

    async def export_app_dsl(self, app_id: str) -> str:
        """Export an app's definition as DSL YAML, for re-import as a Sila agent."""
        ...
