"""FluxImageAdapter — the GPU hand-off + graceful degradation, mocked over
httpx (no GPU/model needed). The FLUX generation itself is validated on the
target machine, not here."""

from __future__ import annotations

import httpx
import pytest

from sila.adapters.image.flux import FluxImageAdapter, NullImageAdapter

_PNG = b"\x89PNG\r\n\x1a\nfake"


def _mock(handler):
    real = httpx.AsyncClient

    def factory(*_a, **_k):
        return real(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0))

    return factory


@pytest.mark.asyncio
async def test_generate_unloads_brain_then_generates_then_releases(monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/generate":  # brain unload
            return httpx.Response(200, json={})
        if request.url.path == "/generate":
            return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
        if request.url.path == "/release":
            return httpx.Response(200, json={"released": True})
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    adapter = FluxImageAdapter("http://img:7810", "http://ollama:11434", "qwen3:14b")
    out = await adapter.generate_images(["a sunrise", "a mountain"])

    assert out == [_PNG, _PNG]
    assert calls[0] == "POST /api/generate"  # brain unloaded FIRST
    assert calls.count("POST /generate") == 2  # one per prompt
    assert calls[-1] == "POST /release"  # VRAM returned to the brain LAST


@pytest.mark.asyncio
async def test_generation_failure_degrades_to_none(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/generate":
            return httpx.Response(503, json={"detail": "OOM"})
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    adapter = FluxImageAdapter("http://img:7810", "http://ollama:11434", "qwen3:14b")
    out = await adapter.generate_images(["x"])
    assert out == [None]  # never raises — caller renders a placeholder


@pytest.mark.asyncio
async def test_brain_unload_failure_does_not_block_generation(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/generate":
            raise httpx.ConnectError("ollama down")
        if request.url.path == "/generate":
            return httpx.Response(200, content=_PNG)
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    adapter = FluxImageAdapter("http://img:7810", "http://ollama:11434", "qwen3:14b")
    out = await adapter.generate_images(["x"])
    assert out == [_PNG]  # unload is best-effort; generation still proceeds


@pytest.mark.asyncio
async def test_null_adapter_returns_all_none() -> None:
    out = await NullImageAdapter().generate_images(["a", "b", "c"])
    assert out == [None, None, None]


@pytest.mark.asyncio
async def test_empty_prompts_short_circuits(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _mock(lambda r: httpx.Response(500)))
    adapter = FluxImageAdapter("http://img:7810", "http://ollama:11434", "qwen3:14b")
    assert await adapter.generate_images([]) == []  # no HTTP at all
