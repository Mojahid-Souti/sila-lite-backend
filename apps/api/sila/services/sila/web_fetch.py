"""Fetch a web page and return its readable text — SILA's "read this URL" tool.

When a user pastes a link and asks SILA to summarize/extract from it, this
fetches the page (SSRF-guarded — never internal IPs or cloud metadata), strips
it to plain text (stdlib HTMLParser, no bs4 — the stack is frozen), and hands the
text back for the model to reason over. Read-only, size- and time-bounded.

Security: the URL runs through validate_external_url (blocks private/link-local/
metadata addresses, http/https only) BEFORE any request. The response is capped
so a hostile server can't exhaust memory. No JS execution — static HTML only.

Complexity: O(len(html)) parse; one bounded HTTP round-trip.
"""

from __future__ import annotations

from html.parser import HTMLParser
from io import StringIO

import httpx
import structlog

from sila.core.security.ssrf_guard import validate_external_url

log = structlog.get_logger(__name__)

_MAX_BYTES = 2_000_000  # 2 MB cap on the fetched body
_MAX_TEXT = 8_000  # chars of readable text handed to the model
_TIMEOUT_S = 20.0
# Headers for reading a PUBLIC page the user asked SILA to open. The previous
# "SilaBot/1.0" identified us as a bot and was 403'd by ordinary sites — measured
# here, ir.omantel.om returned 403 to it and 200 to this, which is why the
# assistant kept reporting "Omantel's IR site blocks direct reads". Accept and
# Accept-Language matter too: some CDNs reject a request with neither.
#
# Scope: this is a read-only fetch of a page a user named, through the SSRF
# guard, with no cookies and no JS. It does not bypass a login or a paywall, and
# nothing here retries around a refusal — a 403 that survives this is honoured.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}
# Elements whose text is noise, never content. NOT `head` — <title> lives there
# and is captured specially; head's other text-bearing children (style/script)
# are skipped by their own tags.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})
_BLOCK_TAGS = frozenset(
    {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section"}
)


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping script/style and inserting line breaks at
    block boundaries so paragraphs don't run together."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out = StringIO()
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self._out.write("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        text = data.strip()
        if text:
            self._out.write(text + " ")

    def text(self) -> str:
        # Collapse the runs of blank lines the block breaks produce.
        lines = [ln.strip() for ln in self._out.getvalue().splitlines()]
        return "\n".join(ln for ln in lines if ln)


async def fetch_readable(url: str) -> tuple[str, str]:
    """Fetch `url` and return (title, readable_text). Raises ValueError on an
    invalid/blocked URL or a fetch failure — the caller turns it into tool text."""
    safe_url = await validate_external_url(url)
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_S, follow_redirects=True, max_redirects=5
        ) as client:
            resp = await client.get(safe_url, headers=_HEADERS)
    except httpx.HTTPError as exc:
        raise ValueError(f"Couldn't reach the page: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        reason = {
            403: "the site refused the request (403)",
            404: "the page does not exist (404)",
            429: "the site is rate-limiting us (429)",
        }.get(resp.status_code, f"the site returned HTTP {resp.status_code}")
        raise ValueError(reason)

    ctype = resp.headers.get("content-type", "")
    body = resp.content[:_MAX_BYTES]
    if "html" not in ctype and "xml" not in ctype:
        # Plain text / JSON / markdown — return as-is (decoded, bounded).
        return "", body.decode("utf-8", errors="replace")[:_MAX_TEXT]

    parser = _TextExtractor()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 — malformed HTML shouldn't 500 the tool
        raise ValueError("Couldn't parse the page content") from exc
    return parser.title.strip(), parser.text()[:_MAX_TEXT]
