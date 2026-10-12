"""OllamaLLMAdapter.stream_chat — NDJSON streaming, tool calls, resilience."""

import json
from collections.abc import Callable

import httpx
import pytest

from sila.adapters.llm.base import LLMDone, LLMGatewayError, LLMTextDelta, LLMToolCall
from sila.adapters.llm.ollama import OllamaLLMAdapter


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> OllamaLLMAdapter:
    a = OllamaLLMAdapter(base_url="http://ollama:11434", model="qwen-test")
    a._client = lambda *_a, **_k: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0)
    )
    return a


def _ndjson(*frames: dict) -> bytes:
    return "".join(json.dumps(f) + "\n" for f in frames).encode()


async def _collect(adapter: OllamaLLMAdapter, **kw) -> list:
    messages = [{"role": "user", "content": "hi"}]
    return [c async for c in adapter.stream_chat(messages=messages, **kw)]


@pytest.mark.asyncio
async def test_streams_text_deltas_then_done() -> None:
    body = _ndjson(
        {"message": {"content": "Hel"}, "done": False},
        {"message": {"content": "lo"}, "done": False},
        {"message": {"content": ""}, "done": True, "prompt_eval_count": 7, "eval_count": 2},
    )
    a = _adapter(lambda _r: httpx.Response(200, content=body))
    chunks = await _collect(a)
    assert [c.text for c in chunks if isinstance(c, LLMTextDelta)] == ["Hel", "lo"]
    done = chunks[-1]
    assert isinstance(done, LLMDone)
    # Back-compat keys still present for existing consumers...
    assert done.usage["prompt_tokens"] == 7
    assert done.usage["completion_tokens"] == 2
    # ...alongside the canonical cost-telemetry split.
    assert done.usage["input_tokens"] == 7
    assert done.usage["output_tokens"] == 2
    assert done.usage["engine"] == "ollama"


@pytest.mark.asyncio
async def test_done_frame_captures_prefill_and_decode_durations() -> None:
    """G2/G4 — Ollama's four duration fields must reach the caller. The previous
    adapter discarded them, which forced GPU 'active time' to be faked from
    wall-clock latency (measurably wrong: a cold request was GPU-busy only 43%
    of its wall clock on this rig)."""
    body = _ndjson(
        {
            "message": {"content": "hi"},
            "done": True,
            "model": "qwen3:14b",
            "prompt_eval_count": 40,
            "prompt_eval_duration": 230_000_000,  # 0.23 s
            "eval_count": 300,
            "eval_duration": 6_640_000_000,  # 6.64 s
            "load_duration": 8_950_000_000,  # 8.95 s
            "total_duration": 15_830_000_000,  # 15.83 s
        },
    )
    a = _adapter(lambda _r: httpx.Response(200, content=body))
    done = (await _collect(a))[-1]
    assert isinstance(done, LLMDone)
    u = done.usage
    assert u["prefill_seconds"] == pytest.approx(0.23)
    assert u["decode_seconds"] == pytest.approx(6.64)
    # active = prefill + decode, NOT the 15.83 s wall clock.
    assert u["active_seconds"] == pytest.approx(6.87)
    assert u["total_seconds"] == pytest.approx(15.83)
    assert u["load_seconds"] == pytest.approx(8.95)
    assert u["model"] == "qwen3:14b"


@pytest.mark.asyncio
async def test_surfaces_tool_calls_with_parsed_arguments() -> None:
    body = _ndjson(
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "search_bazaar", "arguments": {"query": "hr"}}},
                    # Some providers send arguments as a JSON string.
                    {"function": {"name": "navigate", "arguments": '{"page": "bazaar"}'}},
                ],
            },
            "done": False,
        },
        {"message": {"content": ""}, "done": True},
    )
    a = _adapter(lambda _r: httpx.Response(200, content=body))
    calls = [c for c in await _collect(a) if isinstance(c, LLMToolCall)]
    assert calls[0] == LLMToolCall(name="search_bazaar", arguments={"query": "hr"})
    assert calls[1] == LLMToolCall(name="navigate", arguments={"page": "bazaar"})


@pytest.mark.asyncio
async def test_malformed_lines_are_skipped_not_fatal() -> None:
    body = b"not-json\n" + _ndjson(
        {"message": {"content": "ok"}, "done": False},
        {"message": {"content": ""}, "done": True},
    )
    a = _adapter(lambda _r: httpx.Response(200, content=body))
    chunks = await _collect(a)
    assert any(isinstance(c, LLMTextDelta) and c.text == "ok" for c in chunks)
    assert isinstance(chunks[-1], LLMDone)


@pytest.mark.asyncio
async def test_missing_done_frame_still_yields_done() -> None:
    body = _ndjson({"message": {"content": "partial"}, "done": False})
    a = _adapter(lambda _r: httpx.Response(200, content=body))
    chunks = await _collect(a)
    assert isinstance(chunks[-1], LLMDone)


@pytest.mark.asyncio
async def test_http_error_raises_gateway_error() -> None:
    a = _adapter(lambda _r: httpx.Response(500, content=b""))
    with pytest.raises(LLMGatewayError):
        await _collect(a)


@pytest.mark.asyncio
async def test_tools_are_forwarded_in_payload() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, content=_ndjson({"message": {"content": ""}, "done": True}))

    a = _adapter(handler)
    tools = [{"type": "function", "function": {"name": "navigate", "parameters": {}}}]
    await _collect(a, tools=tools)
    assert seen["tools"] == tools
    assert seen["stream"] is True
