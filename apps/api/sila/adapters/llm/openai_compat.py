"""OpenAI-compatible LLM adapter — the swap point for a hosted model.

Speaks the `/chat/completions` dialect, so it drives Alibaba DashScope (Qwen)
and any other OpenAI-compatible endpoint through the same `LLMGateway` the
local Ollama adapter implements. Callers do not change.

⚠ SOVEREIGNTY — this adapter sends prompts, tool arguments and tool results to a
THIRD-PARTY endpoint outside Oman. `infra/policies/pdpl.yaml` denies cloud model
providers under Art. 37-38 and the Cloud Policy, and the `LLMGateway` docstring
requires "explicit, classification-gated opt-in" before any external call. It is
therefore OFF unless `SILA_LLM_PROVIDER=qwen-cloud` is set deliberately, refuses
to construct without a key, and announces itself in the log on every start.
Nothing here relaxes the gatekeeper: agents built through it are still Draft +
Unvetted, and pdpl.yaml is untouched. See `docs/ops/CLOUD_MODEL_EVALUATION.md`
for what is suspended while it runs and what must happen before it could ship.

Complexity: `complete_json` O(1) round-trip; `stream_chat` O(n) over generated
tokens with constant memory (SSE parsed line-by-line, never buffered whole).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import LLMDone, LLMGateway, LLMGatewayError, LLMTextDelta, LLMToolCall

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _usage(raw: dict[str, Any] | None, model: str) -> dict[str, Any]:
    """Normalise OpenAI usage into the canonical cost-telemetry block.

    Only token counts are available — a hosted API reports no prefill/decode
    split, so the GPU-second fields stay ABSENT rather than being invented.
    `analytics_service` treats missing timings as uncovered rather than zero, so
    cost reporting degrades honestly instead of silently reading as free.
    """
    u = raw or {}
    return {
        "input_tokens": int(u.get("prompt_tokens") or 0),
        "output_tokens": int(u.get("completion_tokens") or 0),
        "engine": "openai_compat",
        "model": model,
    }


class OpenAICompatLLMAdapter(LLMGateway):
    """A hosted, OpenAI-dialect chat model. Read the module docstring first."""

    # Declared, not inferred: this adapter exists to reach a third-party
    # endpoint. Paths handling confidential data refuse it (services/okr/residency.py).
    is_local = False

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        provider: str = "qwen-cloud",
    ) -> None:
        if not api_key:
            # Fail loudly at construction. A missing key must never fall back to
            # the local model silently, or a quality comparison would be
            # measuring the wrong engine and reporting the wrong conclusion.
            raise LLMGatewayError(f"{provider}: no API key configured")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._provider = provider
        logger.warning(
            "llm.external_provider_active provider=%s model=%s base_url=%s "
            "(prompts leave the machine; see docs/ops/CLOUD_MODEL_EVALUATION.md)",
            provider,
            model,
            self._base_url,
        )

    def _client(self, timeout: httpx.Timeout | float) -> httpx.AsyncClient:
        # Test seam — mirrors OllamaLLMAdapter so tests can swap a MockTransport.
        return httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        timeout_s: float = 30.0,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response_format: dict[str, Any] = (
            {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": True},
            }
            if schema is not None
            else {"type": "json_object"}
        )
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": response_format,
            "temperature": 0,
            "stream": False,
        }
        try:
            async with self._client(timeout_s) as client:
                resp = await client.post(f"{self._base_url}/chat/completions", json=body)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise LLMGatewayError(
                f"{self._provider} HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMGatewayError(f"{self._provider} transport error: {exc}") from exc

        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMGatewayError(f"{self._provider}: unexpected response shape") from exc
        try:
            return dict(json.loads(_FENCE.sub("", text).strip()))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise LLMGatewayError(f"{self._provider}: response was not JSON") from exc

    async def stream_chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout_s: float = 120.0,
    ) -> AsyncIterator[LLMTextDelta | LLMToolCall | LLMDone]:
        """Stream `/chat/completions` SSE, surfacing text and native tool calls.

        Tool calls arrive as deltas indexed by position and must be reassembled:
        the name lands on the first fragment and the JSON arguments accumulate
        across many. They are emitted only after the stream closes, once each
        call's arguments actually parse — handing the graph a half-built
        argument string would execute a tool on truncated input.
        """
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        # index -> {"id", "name", "args"}, accumulated across delta fragments.
        pending: dict[int, dict[str, str]] = {}
        usage_raw: dict[str, Any] | None = None

        try:
            async with (
                self._client(httpx.Timeout(timeout_s, connect=15.0)) as client,
                client.stream("POST", f"{self._base_url}/chat/completions", json=body) as resp,
            ):
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                    raise LLMGatewayError(f"{self._provider} HTTP {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        # One malformed frame must not kill the stream — the
                        # same resilience rule the Ollama adapter follows.
                        logger.debug("openai_compat: skipped malformed SSE frame")
                        continue
                    if chunk.get("usage"):
                        usage_raw = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield LLMTextDelta(text=text)
                    for call in delta.get("tool_calls") or []:
                        idx = int(call.get("index") or 0)
                        slot = pending.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if call.get("id"):
                            slot["id"] = call["id"]
                        fn = call.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
        except LLMGatewayError:
            raise
        except httpx.HTTPError as exc:
            raise LLMGatewayError(f"{self._provider} transport error: {exc}") from exc

        for idx in sorted(pending):
            slot = pending[idx]
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except json.JSONDecodeError:
                logger.warning(
                    "openai_compat: dropped tool call with unparseable arguments name=%s",
                    slot["name"],
                )
                continue
            yield LLMToolCall(name=slot["name"], arguments=args if isinstance(args, dict) else {})

        yield LLMDone(usage=_usage(usage_raw, self._model))
