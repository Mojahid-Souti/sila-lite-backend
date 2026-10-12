"""Ollama LLM adapter — local-only inference via `POST /api/chat`.

`complete_json` forces `format: "json"` so the model is constrained to emit
valid JSON (parsed defensively — models occasionally wrap output in fences).
`stream_chat` streams NDJSON lines with `stream: true` and surfaces native
tool calls (qwen2.5 supports function calling).

Complexity: `complete_json` O(1) round-trip; `stream_chat` O(n) over tokens,
constant memory.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from sila.core.cost_telemetry import usage_from_ollama

from .base import LLMDone, LLMGateway, LLMGatewayError, LLMTextDelta, LLMToolCall

logger = logging.getLogger(__name__)

# Strip ```json fences models sometimes leak through despite format=json.
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


# Hosts that are this machine or its compose network. A single-label name is a
# compose service ("ollama"): it cannot resolve on the public internet.
_LOCAL_HOSTNAMES = frozenset({"localhost", "host.docker.internal"})
# Named explicitly rather than via `ip.is_private`, which also counts reserved
# and documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24) as
# "private". Local means loopback, the RFC 1918 ranges, or an IPv6 ULA.
_LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7")
)


class OllamaLLMAdapter(LLMGateway):
    @property
    def is_local(self) -> bool:
        """Does this gateway send prompts only to this host or its private network?

        Read by paths that handle confidential data (see
        `services/okr/residency.py`). Decided from the endpoint, not the class:
        Ollama pointed at a public hostname is a hosted model like any other, so
        a dotted name that is not an IP literal is NOT local. No DNS lookup —
        the answer must not depend on what a resolver says today.
        """
        host = (urlsplit(self._base_url).hostname or "").lower()
        if host in _LOCAL_HOSTNAMES:
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return bool(host) and "." not in host
        return any(address in network for network in _LOCAL_NETWORKS)

    def __init__(
        self,
        base_url: str,
        model: str,
        num_ctx: int | None = None,
        keep_alive: str = "2h",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._num_ctx = num_ctx
        # Keep the model resident in VRAM between turns. Ollama's default (5m)
        # unloaded it mid-conversation → a 10–30s "Loading qwen2.5:7b" stall on
        # the next message that read as a freeze and could outlive the WS.
        self._keep_alive = keep_alive
        # Qwen3 quirks (model-specific → adapter layer): thinking is disabled via
        # the /no_think soft switch, and the model card's NON-thinking sampling
        # (temp .7 / top_p .8 / top_k 20 / min_p 0) applies — greedy/low-temp
        # settings degrade Qwen3 into repetition.
        self._qwen3 = model.lower().startswith("qwen3")

    def _client(self, timeout: httpx.Timeout | float) -> httpx.AsyncClient:
        # Test seam — tests swap in httpx.MockTransport (same pattern as DifyAdapter).
        return httpx.AsyncClient(timeout=timeout)

    async def is_available(self, timeout_s: float = 5.0) -> bool:
        """Is this model pulled on this Ollama, and is the Ollama answering?

        A confidential conversation asks before it sends anything: with no
        local model it refuses, rather than fall back to a hosted one.
        """
        try:
            async with self._client(timeout_s) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                models = resp.json().get("models") or []
        except (httpx.HTTPError, ValueError):
            return False
        names = {str(m.get("name", "")) for m in models if isinstance(m, dict)}
        wanted = self._model if ":" in self._model else f"{self._model}:latest"
        return self._model in names or wanted in names

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        timeout_s: float = 30.0,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._qwen3:
            # A <think> block before the JSON would break parsing — force it off.
            system_prompt = f"{system_prompt}\n/no_think"
        payload = {
            "model": self._model,
            # A JSON Schema constrains decoding itself (Ollama >= 0.5): values
            # outside it cannot be produced. Plain "json" only forces syntax.
            "format": schema if schema is not None else "json",
            "stream": False,
            "keep_alive": self._keep_alive,
            # Ollama's REAL thinking switch — the /no_think soft prompt is ignored
            # by its qwen3 template (measured: 280 hidden reasoning tokens for a
            # 3-word answer). Unknown fields are ignored by older servers.
            **({"think": False} if self._qwen3 else {}),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {
                # Low temperature for deterministic security findings — we
                # don't want creative interpretations of "is this PII".
                "temperature": 0.1,
            },
        }
        try:
            async with self._client(timeout_s) as client:
                resp = await client.post(f"{self._base_url}/api/chat", json=payload)
                resp.raise_for_status()
                body = resp.json()
        except httpx.TimeoutException as e:
            raise LLMGatewayError(f"LLM call timed out after {timeout_s}s") from e
        except httpx.HTTPError as e:
            raise LLMGatewayError(f"LLM HTTP error: {e}") from e

        content = body.get("message", {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise LLMGatewayError("LLM returned empty content")

        cleaned = _FENCE.sub("", content).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning("LLM returned non-JSON content: %s", cleaned[:200])
            raise LLMGatewayError(f"LLM returned malformed JSON: {e}") from e

        if not isinstance(parsed, dict):
            raise LLMGatewayError(f"LLM returned non-object JSON: {type(parsed).__name__}")
        return parsed

    async def stream_chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout_s: float = 120.0,
    ) -> AsyncIterator[LLMTextDelta | LLMToolCall | LLMDone]:
        """Stream `/api/chat` NDJSON. Yields text deltas, tool calls, then Done.

        Malformed lines are skipped (logged), matching the Dify adapter's
        resilience pattern — a single bad frame must not kill the stream.
        """
        if self._qwen3 and messages and messages[0].get("role") == "system":
            # Non-thinking mode: keeps the tool loop snappy and stops the
            # methodology riffing where invented numbers creep in.
            head = {**messages[0], "content": f"{messages[0].get('content', '')}\n/no_think"}
            messages = [head, *messages[1:]]
        sampling: dict[str, Any] = (
            {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0}
            if self._qwen3
            else {"temperature": 0.3}
        )
        payload: dict[str, Any] = {
            "model": self._model,
            "stream": True,
            "messages": messages,
            "keep_alive": self._keep_alive,
            # Ollama's REAL thinking switch for qwen3 (the /no_think soft prompt
            # is ignored by its template — 280 hidden tokens per short answer).
            **({"think": False} if self._qwen3 else {}),
            # num_ctx matters: Ollama's default window (4096) silently truncates
            # SILA's long system prompt + tool results, which reads as amnesia.
            "options": sampling | ({"num_ctx": self._num_ctx} if self._num_ctx else {}),
        }
        if tools:
            payload["tools"] = tools

        # Bounded read timeout: a wedged Ollama used to hang the turn FOREVER
        # with zero logs (live bug — "it stuck" on consecutive turns). 240s
        # covers a cold 14B load and busy-GPU token gaps; a real stall now
        # surfaces as a retryable error instead of a frozen chat.
        timeout = httpx.Timeout(timeout_s, read=240.0)
        try:
            async with (
                self._client(timeout) as client,
                client.stream("POST", f"{self._base_url}/api/chat", json=payload) as resp,
            ):
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed Ollama frame: %s", line[:120])
                        continue

                    message = frame.get("message", {}) or {}
                    for call in message.get("tool_calls") or []:
                        fn = call.get("function", {}) or {}
                        args = fn.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        yield LLMToolCall(name=fn.get("name", ""), arguments=args or {})
                    text = message.get("content")
                    if isinstance(text, str) and text:
                        yield LLMTextDelta(text=text)

                    if frame.get("done"):
                        # Carry the FULL cost block (tokens + prefill/decode
                        # durations). The previous version kept only the two
                        # counts and discarded every duration, which is why
                        # active GPU time had to be faked from wall clock.
                        usage = usage_from_ollama(frame)
                        # Back-compat keys for existing consumers.
                        usage["prompt_tokens"] = usage["input_tokens"]
                        usage["completion_tokens"] = usage["output_tokens"]
                        yield LLMDone(usage=usage)
                        return
        except httpx.TimeoutException as e:
            raise LLMGatewayError(
                "the model stopped responding (no tokens for 240s) — ask the user "
                "to send the message again; the session state is intact"
            ) from e
        except httpx.HTTPError as e:
            raise LLMGatewayError(f"LLM stream HTTP error: {e}") from e
        # Stream ended without a done frame — still signal completion.
        yield LLMDone()
