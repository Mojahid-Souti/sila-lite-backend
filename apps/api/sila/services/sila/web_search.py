"""Search the web — SILA's "look this up" tool, via a self-hosted SearXNG.

fetch_url reads a page the user names; this finds the pages in the first place.
Queries go to our OWN SearXNG meta-search container (never a third-party API), so
the user's search intent stays on local infrastructure — the sovereignty /
PDPL-by-design line the whole platform holds. SearXNG aggregates public engines
and returns clean JSON; we hand SILA the top results (title, url, snippet), and it
can chain into fetch_url to open the best hit.

Complexity: O(results) formatting; one bounded HTTP round-trip to the local box.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import structlog

from sila.core.config import settings

log = structlog.get_logger(__name__)

_TIMEOUT_S = 12.0
# Measured, not guessed: the same query came back in 17s, 21s, 26s and 58s on
# consecutive runs. The variance is server-side, so the ceiling has to clear the
# slow tail — 90s was inside it, and every timeout cost the whole turn.
# Two attempts have to FIT INSIDE the 300s tool cap in tool_registry, with the
# SearXNG attempt and the response walk still to come. At 150 they summed to
# exactly 300, so the second attempt was always killed mid-flight — the retry
# could never once succeed, and a dead search cost the full five minutes before
# saying so. 120 leaves real headroom: 12 + 240 against 300.
_PAID_TIMEOUT_S = 120.0
# A timed-out call is usually just an unlucky one; the retry lands fast.
_PAID_ATTEMPTS = 2
_MAX_RESULTS = 8  # top-N handed to the model (keeps the prompt bounded)
_SNIPPET_MAX = 300

# Repeat-query cache. The model re-runs near-identical searches within a turn —
# one observed turn fired fourteen. Every repeat is another request to engines
# that suspend us for exactly that ("brave: too many requests", "yep: access
# denied 403"), so the looping was manufacturing its own blocks. Serving the
# repeat from memory cuts the volume without changing what the model sees.
# Bounded dict, evicted oldest-first; O(1) lookup, O(1) amortised insert.
_CACHE_TTL_S = 300.0
_CACHE_MAX = 128
_cache: dict[str, tuple[float, list[SearchHit]]] = {}


class SearchUnavailableError(ValueError):
    """Every engine refused us — distinct from "the web has no match for this".

    Reported separately so SILA can say "search is blocked right now" instead of
    "no results", which reads as a fact about the world and sends the model into
    rephrasing a query that was never the problem.
    """


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


# ---------------------------------------------------------------------------
# Paid fallback — DashScope's built-in web_search, used ONLY when the free path
# comes back blocked. It costs $10 per 1,000 calls and takes ~20-40 s (it
# searches, reads pages and synthesises server-side), so it is the safety net,
# never the default. A daily counter caps the spend; it resets on the date.
# ---------------------------------------------------------------------------
_paid_day = ""
_paid_used = 0


def paid_search_usage() -> tuple[int, int]:
    """(used today, daily cap) — for the cost report and the tests."""
    return (_paid_used if _paid_day == date.today().isoformat() else 0), int(
        settings.web_search_paid_daily_cap
    )


def _claim_paid_call() -> bool:
    """Take one unit of today's budget. False when the cap is reached.

    Deliberately a plain module counter: this is one API process, and a paid
    call must be refused rather than queued. If the deployment ever scales past
    one process this needs to move to Postgres, or each process gets its own cap.
    """
    global _paid_day, _paid_used
    today = date.today().isoformat()
    if _paid_day != today:
        _paid_day, _paid_used = today, 0
    if _paid_used >= int(settings.web_search_paid_daily_cap):
        return False
    _paid_used += 1
    return True


async def _search_paid(query: str, *, count: int) -> list[SearchHit]:
    """DashScope /responses with the built-in web_search + web_extractor tools.

    Asks for the sources rather than an essay: the synthesis is the slow part and
    SILA is going to write its own answer from these hits anyway.
    """
    base = settings.qwen_base_url.rstrip("/")
    body = {
        "model": settings.qwen_model,
        "input": (
            f"Search the web for: {query}\n\n"
            "Return ONLY a compact list of the most relevant sources — title and "
            "URL and one line of what each says. Do not write an essay."
        ),
        "tools": [{"type": "web_search"}, {"type": "web_extractor"}],
    }
    headers = {
        "Authorization": f"Bearer {settings.qwen_api_key}",
        "Content-Type": "application/json",
    }
    last: Exception | None = None
    for attempt in range(_PAID_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=_PAID_TIMEOUT_S, headers=headers) as client:
                resp = await client.post(f"{base}/responses", json=body)
        except httpx.TimeoutException as exc:
            last = exc
            log.warning("sila.web_search.paid_timeout", attempt=attempt + 1)
            continue
        if resp.status_code >= 400:
            raise SearchUnavailableError(f"Paid search returned HTTP {resp.status_code}.")
        return _hits_from_response(resp.json())[:count]
    raise SearchUnavailableError("Paid search timed out.") from last


def _hits_from_response(payload: dict[str, Any]) -> list[SearchHit]:
    """Pull (title, url, snippet) out of a Responses payload.

    Walks the tree for url-bearing nodes rather than pinning an exact path — the
    built-in tools nest their results differently from the assistant message, and
    a shape change should cost us relevance, not an exception.
    """
    hits: list[SearchHit] = []
    seen: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            url = node.get("url") or node.get("link")
            if isinstance(url, str) and url.startswith("http") and url not in seen:
                seen.add(url)
                title = str(node.get("title") or node.get("site_name") or url)[:200]
                snippet = str(node.get("snippet") or node.get("content") or "")[:_SNIPPET_MAX]
                hits.append(SearchHit(title=title, url=url, snippet=snippet))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return hits


def _remember(key: str, now: float, hits: list[SearchHit]) -> None:
    """Cache a result set, evicting the oldest when full. Paid results are cached
    like free ones — a repeat of a search that cost money must never cost it
    twice."""
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)), None)
    _cache[key] = (now, hits)


async def search_web(query: str, *, count: int = _MAX_RESULTS) -> list[SearchHit]:
    """Top results for `query`: the local SearXNG first, the paid API only if the
    free engines are blocked. Raises ValueError on an empty query, and
    SearchUnavailableError when both paths are unavailable."""
    q = (query or "").strip()
    if not q:
        raise ValueError("The search query was empty.")
    key = q.casefold()
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1][:count]
    base = settings.searxng_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.get(
                f"{base}/search",
                params={"q": q, "format": "json", "safesearch": "1"},
                headers={"User-Agent": "SilaBot/1.0"},
            )
    except httpx.HTTPError as exc:
        raise ValueError(f"Search is unavailable right now ({type(exc).__name__}).") from exc
    if resp.status_code >= 400:
        raise ValueError(f"Search returned HTTP {resp.status_code}.")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise ValueError("Search returned an unreadable response.") from exc

    hits: list[SearchHit] = []
    for r in payload.get("results", []):
        if not isinstance(r, dict):
            continue
        url = str(r.get("url", "")).strip()
        title = str(r.get("title", "")).strip()
        if not url or not title:
            continue
        snippet = str(r.get("content", "")).strip()[:_SNIPPET_MAX]
        hits.append(SearchHit(title=title, url=url, snippet=snippet))
        if len(hits) >= count:
            break

    if not hits:
        # Distinguish "nothing matched" from "every engine refused us". SearXNG
        # reports the latter in `unresponsive_engines`, and conflating them sent
        # the model rephrasing a query that was never the problem.
        down = payload.get("unresponsive_engines") or []
        if down:
            names = ", ".join(sorted({str(d[0]) for d in down if d}))
            log.warning("sila.web_search.engines_down", engines=names, query=q[:80])
            # Free search is blocked, not empty — this is exactly what the paid
            # fallback exists for. Budget-checked, and a failure there falls
            # through to the same honest "search is unavailable" as before.
            if settings.web_search_paid_fallback and settings.qwen_api_key:
                if not _claim_paid_call():
                    used, cap = paid_search_usage()
                    log.warning("sila.web_search.paid_cap_reached", used=used, cap=cap)
                    raise SearchUnavailableError(
                        f"Free search is blocked and the paid fallback has used its "
                        f"daily budget ({used}/{cap} calls). Search is unavailable "
                        "until tomorrow."
                    )
                try:
                    paid = await _search_paid(q, count=count)
                except (httpx.HTTPError, SearchUnavailableError, ValueError) as exc:
                    log.warning("sila.web_search.paid_failed", error=type(exc).__name__)
                else:
                    if paid:
                        used, cap = paid_search_usage()
                        log.info("sila.web_search.paid_used", used=used, cap=cap, hits=len(paid))
                        _remember(key, now, paid)
                        return paid
            raise SearchUnavailableError(
                f"Every search engine is currently refusing requests ({names}). "
                "This is a rate limit, not an empty web — rephrasing will not help. "
                "Do NOT guess URLs and fetch them: a guessed address returns a 404 "
                "page, and reading 404s is worse than saying search is unavailable. "
                "Tell the user search is down, and offer to continue with data they "
                "supply or to retry shortly."
            )

    if hits:
        _remember(key, now, hits)
    return hits
