"""LLM gateway interface — Rule 5 swap point.

Concrete adapters (Ollama now, LiteLLM/vLLM/Bedrock later) implement this
Protocol so callers (scanner, chat-to-build wizard, SILA concierge) never
depend on a specific provider.

Two capabilities:
  - `complete_json` — single-shot, strict-JSON output (gatekeeper et al.)
  - `stream_chat`   — token streaming + native tool/function calling
    (SILA Voice phase, 16A). Chunks are small frozen dataclasses so callers
    can pattern-match without provider-specific parsing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMGatewayError(Exception):
    """Raised when the underlying LLM call fails or returns malformed JSON.

    Callers should catch this and fail-closed (record-but-continue), never
    let it bubble up and abort the whole vetting run.
    """


@dataclass(frozen=True)
class LLMTextDelta:
    """A streamed fragment of assistant text."""

    text: str


@dataclass(frozen=True)
class LLMToolCall:
    """The model requested a tool invocation (arguments already parsed)."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMDone:
    """Stream finished; carries token usage when the provider reports it.

    Values are mixed-type (int token counts, float second-durations, str model)
    because this now carries the full cost-telemetry block from
    `core.cost_telemetry.usage_from_ollama` — input/output tokens PLUS the
    prefill/decode durations the cost model needs, not just a blended count.
    """

    usage: dict[str, Any] = field(default_factory=dict)


LLMChunk = LLMTextDelta | LLMToolCall | LLMDone


class LLMGateway(Protocol):
    """Local-first LLM access. All implementations must keep inference local
    or behind a workspace-trusted endpoint — never call an external API
    without explicit, classification-gated opt-in.

    Complexity: one network round-trip per `complete_json`; `stream_chat` is
    O(n) over generated tokens with constant memory (no buffering).
    """

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        timeout_s: float = 30.0,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue a single chat completion forced to JSON output.

        With `schema` (a JSON Schema), implementations that can constrain
        decoding to it must: the model then cannot produce a value the schema
        does not allow. Callers still validate what comes back.

        Returns the parsed JSON object. Raises `LLMGatewayError` on timeout,
        HTTP failure, or unparseable response — never returns partial data.
        """
        ...

    def stream_chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout_s: float = 120.0,
    ) -> AsyncIterator[LLMChunk]:
        """Stream a chat completion, optionally exposing tools to the model.

        `messages` follow the OpenAI/Ollama chat shape ({role, content, ...});
        `tools` are JSON-schema function definitions. Yields `LLMTextDelta`
        for text, `LLMToolCall` when the model invokes a tool, and a final
        `LLMDone`. Raises `LLMGatewayError` on transport failure.
        """
        ...
