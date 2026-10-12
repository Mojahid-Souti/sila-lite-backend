"""SILA's web_search tool — queries the self-hosted SearXNG."""

from __future__ import annotations

import httpx
import pytest

from sila.services.sila import web_search


def _mock(handler):  # type: ignore[no-untyped-def]
    real = httpx.AsyncClient

    def factory(*_a, **_kw):  # type: ignore[no-untyped-def]
        return real(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0))

    return factory


_PAYLOAD = {
    "results": [
        {"title": "Result one", "url": "https://a.example/1", "content": "First snippet."},
        {"title": "Result two", "url": "https://b.example/2", "content": "Second snippet."},
        {"title": "", "url": "https://c.example/3", "content": "no title -> dropped"},
        {"title": "No url", "url": "", "content": "dropped"},
    ]
}


async def test_returns_top_hits(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["q"] = req.url.params.get("q", "")
        seen["format"] = req.url.params.get("format", "")
        return httpx.Response(200, json=_PAYLOAD)

    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    hits = await web_search.search_web("oman news")
    assert seen == {"q": "oman news", "format": "json"}
    # Rows missing a title or url are dropped.
    assert [h.url for h in hits] == ["https://a.example/1", "https://b.example/2"]
    assert hits[0].title == "Result one" and hits[0].snippet == "First snippet."


async def test_empty_query_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        await web_search.search_web("   ")


async def test_http_error_becomes_value_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    with pytest.raises(ValueError, match="502"):
        await web_search.search_web("q")


async def test_count_caps_results(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    many = {
        "results": [{"title": f"t{i}", "url": f"https://x/{i}", "content": ""} for i in range(20)]
    }

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=many)

    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    hits = await web_search.search_web("q", count=3)
    assert len(hits) == 3


async def test_web_search_tool_formats_results(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import uuid

    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext, execute_tool

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_PAYLOAD)

    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )
    res = await execute_tool("web_search", ctx, {"query": "oman news"})
    assert "Result one" in res.content and "https://a.example/1" in res.content
    assert "Searched the web" in (res.summary or "")
    # The citations ride the RESULT, not the prose. Relying on the model to
    # repeat the markdown meant the source list appeared only sometimes; the UI
    # renders these deterministically under the step.
    sources = (res.meta or {}).get("sources") or []
    assert [s["url"] for s in sources] == ["https://a.example/1", "https://b.example/2"]
    assert [s["domain"] for s in sources] == ["a.example", "b.example"]
    assert all(s["title"] for s in sources)
    empty = await execute_tool("web_search", ctx, {"query": ""})
    assert "No search query" in empty.content


@pytest.mark.asyncio
async def test_repeat_queries_are_served_from_cache(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The model re-runs near-identical searches within a turn — one observed
    turn fired fourteen. Every repeat is another request to engines that suspend
    us for exactly that, so the loop was manufacturing its own blocks."""
    from sila.services.sila import web_search as ws

    ws._cache.clear()
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"results": [{"url": "https://a.example/1", "title": "One", "content": "x"}]},
        )

    monkeypatch.setattr(ws.httpx, "AsyncClient", _mock(handler))
    assert len(await ws.search_web("omantel results")) == 1
    assert len(await ws.search_web("Omantel Results")) == 1  # case-folded hit
    assert calls["n"] == 1, "the repeat must not reach the network"
    ws._cache.clear()


@pytest.mark.asyncio
async def test_all_engines_blocked_is_not_reported_as_no_results(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """ "No results" reads as a fact about the world; being rate-limited is not.
    Conflating them sent the model rephrasing a query that was never at fault,
    firing more requests at engines blocking us for request volume."""
    from sila.services.sila import web_search as ws

    ws._cache.clear()

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [],
                "unresponsive_engines": [["brave", "Suspended: too many requests"], ["yep", "403"]],
            },
        )

    monkeypatch.setattr(ws.httpx, "AsyncClient", _mock(handler))
    with pytest.raises(ws.SearchUnavailableError, match="brave, yep"):
        await ws.search_web("anything")
    ws._cache.clear()


async def test_blocked_free_search_falls_back_to_paid(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Free engines blocking is exactly what the paid fallback is for. It must
    NOT fire on a genuine no-match — that would spend money to confirm the web
    has nothing."""
    from sila.services.sila import web_search as ws

    ws._cache.clear()

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [], "unresponsive_engines": [["brave", "Suspended"]]},
        )

    monkeypatch.setattr(ws.httpx, "AsyncClient", _mock(handler))
    monkeypatch.setattr(ws.settings, "web_search_paid_fallback", True)
    monkeypatch.setattr(ws.settings, "qwen_api_key", "k")

    called: list[str] = []

    async def fake_paid(q, *, count):  # type: ignore[no-untyped-def]
        called.append(q)
        return [ws.SearchHit(title="T", url="https://x.example/1", snippet="s")]

    monkeypatch.setattr(ws, "_search_paid", fake_paid)
    hits = await ws.search_web("omantel results")
    assert called == ["omantel results"]
    assert hits[0].url == "https://x.example/1"
    ws._cache.clear()


async def test_paid_fallback_does_not_fire_on_a_genuine_no_match(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Engines answered and found nothing. Paying to re-confirm that is waste."""
    from sila.services.sila import web_search as ws

    ws._cache.clear()
    monkeypatch.setattr(
        ws.httpx,
        "AsyncClient",
        _mock(lambda _r: httpx.Response(200, json={"results": [], "unresponsive_engines": []})),
    )
    monkeypatch.setattr(ws.settings, "web_search_paid_fallback", True)
    monkeypatch.setattr(ws.settings, "qwen_api_key", "k")

    async def boom(q, *, count):  # type: ignore[no-untyped-def]
        raise AssertionError("paid search must not run when engines answered")

    monkeypatch.setattr(ws, "_search_paid", boom)
    assert await ws.search_web("nothing matches this") == []
    ws._cache.clear()


async def test_paid_fallback_respects_the_daily_cap(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """$10 per 1,000 calls with a model that re-searches is how a bill runs
    away. The cap refuses rather than queues."""
    from sila.services.sila import web_search as ws

    ws._cache.clear()
    monkeypatch.setattr(ws.settings, "web_search_paid_daily_cap", 2)
    monkeypatch.setattr(ws, "_paid_day", "")
    monkeypatch.setattr(ws, "_paid_used", 0)
    assert ws._claim_paid_call() is True
    assert ws._claim_paid_call() is True
    assert ws._claim_paid_call() is False, "the third call must be refused"
    used, cap = ws.paid_search_usage()
    assert (used, cap) == (2, 2)


def test_hits_are_parsed_from_a_nested_responses_payload() -> None:
    """The built-in tools nest results differently from the assistant message,
    so the parser walks for url-bearing nodes rather than pinning a path."""
    from sila.services.sila.web_search import _hits_from_response

    payload = {
        "output": [
            {"content": [{"annotations": [{"url": "https://a.example/1", "title": "A"}]}]},
            {"tool_result": {"results": [{"url": "https://b.example/2", "site_name": "B"}]}},
            {"content": [{"text": "no url here"}]},
        ]
    }
    hits = _hits_from_response(payload)
    assert [h.url for h in hits] == ["https://a.example/1", "https://b.example/2"]
    assert hits[0].title == "A"
