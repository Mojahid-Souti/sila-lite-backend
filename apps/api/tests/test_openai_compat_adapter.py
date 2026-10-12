"""The hosted OpenAI-compatible adapter — streaming, tool reassembly, guards.

Runs against a MockTransport, so it never touches the network or needs a key.
The live probe against DashScope lives in docs/ops/CLOUD_MODEL_EVALUATION.md.
"""

import httpx
import pytest

from sila.adapters.llm.base import LLMDone, LLMGatewayError, LLMTextDelta, LLMToolCall
from sila.adapters.llm.openai_compat import OpenAICompatLLMAdapter

BASE = "https://example.invalid/v1"


def _adapter(handler: httpx.MockTransport, model: str = "qwen3.8-27b") -> OpenAICompatLLMAdapter:
    gw = OpenAICompatLLMAdapter(base_url=BASE, model=model, api_key="test-key")
    gw._client = lambda timeout: httpx.AsyncClient(transport=handler, timeout=timeout)  # type: ignore[method-assign]
    return gw


def _sse(*frames: str) -> bytes:
    return "".join(f"data: {f}\n\n" for f in frames).encode()


def test_missing_key_fails_loudly() -> None:
    """A blank key must not silently fall back to the local model — a quality
    comparison would then be measuring the wrong engine entirely."""
    with pytest.raises(LLMGatewayError, match="no API key"):
        OpenAICompatLLMAdapter(base_url=BASE, model="m", api_key="")


@pytest.mark.asyncio
async def test_stream_yields_text_then_usage() -> None:
    body = _sse(
        '{"choices":[{"delta":{"content":"Hel"}}]}',
        '{"choices":[{"delta":{"content":"lo"}}]}',
        '{"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":2}}',
        "[DONE]",
    )
    gw = _adapter(httpx.MockTransport(lambda _r: httpx.Response(200, content=body)))
    chunks = [c async for c in gw.stream_chat(messages=[{"role": "user", "content": "hi"}])]
    assert "".join(c.text for c in chunks if isinstance(c, LLMTextDelta)) == "Hello"
    done = chunks[-1]
    assert isinstance(done, LLMDone)
    assert done.usage["input_tokens"] == 11
    assert done.usage["output_tokens"] == 2
    # A hosted API reports no prefill/decode split; inventing zeros would make
    # cloud turns look free next to metered local ones.
    assert "prefill_seconds" not in done.usage
    assert "decode_seconds" not in done.usage


@pytest.mark.asyncio
async def test_tool_call_arguments_are_reassembled_across_frames() -> None:
    """Arguments arrive as many fragments. Emitting early would run a tool on a
    truncated JSON string."""
    body = _sse(
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"create_agent_workflow","arguments":"{\\"goal\\":"}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"\\"analyse invest"}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ments\\"}"}}]}}]}',
        "[DONE]",
    )
    gw = _adapter(httpx.MockTransport(lambda _r: httpx.Response(200, content=body)))
    calls = [
        c
        async for c in gw.stream_chat(messages=[{"role": "user", "content": "build"}])
        if isinstance(c, LLMToolCall)
    ]
    assert len(calls) == 1
    assert calls[0].name == "create_agent_workflow"
    assert calls[0].arguments == {"goal": "analyse investments"}


@pytest.mark.asyncio
async def test_unparseable_tool_arguments_are_dropped_not_guessed() -> None:
    """A truncated stream must not hand the graph half a call."""
    body = _sse(
        '{"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"name":"hire_agent","arguments":"{\\"agent_id\\":"}}]}}]}',
        "[DONE]",
    )
    gw = _adapter(httpx.MockTransport(lambda _r: httpx.Response(200, content=body)))
    chunks = [c async for c in gw.stream_chat(messages=[{"role": "user", "content": "x"}])]
    assert not [c for c in chunks if isinstance(c, LLMToolCall)]
    assert isinstance(chunks[-1], LLMDone)  # the turn still completes


@pytest.mark.asyncio
async def test_malformed_frame_does_not_kill_the_stream() -> None:
    body = _sse(
        '{"choices":[{"delta":{"content":"a"}}]}',
        "{not json",
        '{"choices":[{"delta":{"content":"b"}}]}',
        "[DONE]",
    )
    gw = _adapter(httpx.MockTransport(lambda _r: httpx.Response(200, content=body)))
    chunks = [c async for c in gw.stream_chat(messages=[{"role": "user", "content": "x"}])]
    assert "".join(c.text for c in chunks if isinstance(c, LLMTextDelta)) == "ab"


@pytest.mark.asyncio
async def test_http_error_becomes_a_gateway_error() -> None:
    gw = _adapter(httpx.MockTransport(lambda _r: httpx.Response(401, text="bad key")))
    with pytest.raises(LLMGatewayError, match="401"):
        _ = [c async for c in gw.stream_chat(messages=[{"role": "user", "content": "x"}])]


@pytest.mark.asyncio
async def test_complete_json_parses_and_strips_fences() -> None:
    payload = {"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]}
    gw = _adapter(httpx.MockTransport(lambda _r: httpx.Response(200, json=payload)))
    assert await gw.complete_json(system_prompt="s", user_prompt="u") == {"ok": True}


@pytest.mark.asyncio
async def test_complete_json_rejects_non_json() -> None:
    payload = {"choices": [{"message": {"content": "sorry, no"}}]}
    gw = _adapter(httpx.MockTransport(lambda _r: httpx.Response(200, json=payload)))
    with pytest.raises(LLMGatewayError, match="not JSON"):
        await gw.complete_json(system_prompt="s", user_prompt="u")


def test_local_is_the_default_provider() -> None:
    """An unset or unknown provider must never route to the cloud. The sovereign
    path is the default; the hosted one is an explicit opt-in."""
    from sila.core.config import Settings

    assert Settings().sila_llm_provider == "ollama"
