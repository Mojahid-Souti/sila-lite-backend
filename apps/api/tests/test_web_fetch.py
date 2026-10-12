"""SILA's fetch_url tool — read a page the user names, SSRF-guarded."""

from __future__ import annotations

import httpx
import pytest

from sila.services.sila import web_fetch


def _mock(handler):  # type: ignore[no-untyped-def]
    """Patch httpx.AsyncClient in web_fetch with a MockTransport (real kwargs dropped)."""
    real = httpx.AsyncClient

    def factory(*_a, **_kw):  # type: ignore[no-untyped-def]
        return real(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0))

    return factory


async def _passthrough(url, **_kw):  # type: ignore[no-untyped-def]
    return url


def _stub_guard(monkeypatch):  # type: ignore[no-untyped-def]
    """Bypass the DNS-resolving SSRF guard for fetch-logic tests (the guard has its
    own test below)."""
    monkeypatch.setattr(web_fetch, "validate_external_url", _passthrough)


async def test_html_is_stripped_to_readable_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    html = (
        "<html><head><title>Report</title><style>.x{color:red}</style></head>"
        "<body><script>evil()</script><h1>Q3 Results</h1>"
        "<p>Revenue grew 20%.</p><p>Costs fell.</p></body></html>"
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    _stub_guard(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    title, text = await web_fetch.fetch_readable("https://example.com/report")
    assert title == "Report"
    assert "Q3 Results" in text and "Revenue grew 20%." in text and "Costs fell." in text
    assert "evil()" not in text and "color:red" not in text  # script/style dropped


async def test_ssrf_guard_blocks_private_urls() -> None:
    for bad in (
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://localhost:8001/",
        "ftp://example.com/x",  # scheme
    ):
        with pytest.raises(ValueError):
            await web_fetch.fetch_readable(bad)


async def test_non_html_returned_as_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text='{"ok": true}', headers={"content-type": "application/json"}
        )

    _stub_guard(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    title, text = await web_fetch.fetch_readable("https://api.example.com/data.json")
    assert title == "" and text == '{"ok": true}'


async def test_http_error_becomes_value_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    _stub_guard(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    with pytest.raises(ValueError, match="404"):
        await web_fetch.fetch_readable("https://example.com/missing")


async def test_text_is_bounded(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    big = "<html><body>" + ("<p>word</p>" * 5000) + "</body></html>"

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=big, headers={"content-type": "text/html"})

    _stub_guard(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    _, text = await web_fetch.fetch_readable("https://example.com/big")
    assert len(text) <= web_fetch._MAX_TEXT


async def test_fetch_url_tool_wraps_result(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import uuid

    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext, execute_tool

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<title>Hi</title><body><p>Hello world</p></body>",
            headers={"content-type": "text/html"},
        )

    _stub_guard(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", _mock(handler))
    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )
    res = await execute_tool("fetch_url", ctx, {"url": "https://example.com"})
    assert "Hello world" in res.content and "Read Hi" in (res.summary or "")
    # An empty URL is refused, not fetched.
    empty = await execute_tool("fetch_url", ctx, {"url": ""})
    assert "No URL" in empty.content


async def test_request_does_not_announce_itself_as_a_bot(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The old "SilaBot/1.0" User-Agent was 403'd by ordinary sites — measured,
    ir.omantel.om returned 403 to it and 200 to a browser UA, which is why the
    assistant kept reporting "Omantel's IR site blocks direct reads" on a page
    that reads fine. Accept/Accept-Language go with it: some CDNs reject a
    request carrying neither."""
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in req.headers.items()})
        return httpx.Response(200, html="<html><body><p>hello</p></body></html>")

    monkeypatch.setattr(web_fetch.httpx, "AsyncClient", _mock(handler))
    await web_fetch.fetch_readable("https://example.com/page")

    assert "silabot" not in seen.get("user-agent", "").lower()
    assert "mozilla" in seen.get("user-agent", "").lower()
    assert seen.get("accept")
    assert seen.get("accept-language")


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(403, "refused"), (404, "does not exist"), (429, "rate-limiting")],
)
async def test_refusals_say_which_refusal(monkeypatch, status, fragment) -> None:  # type: ignore[no-untyped-def]
    """ "Fetch failed" told the user nothing and the model nothing it could act
    on. A 403 is a different problem from a 404 and should read like one."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    monkeypatch.setattr(web_fetch.httpx, "AsyncClient", _mock(handler))
    with pytest.raises(ValueError, match=fragment):
        await web_fetch.fetch_readable("https://example.com/x")
