"""skills-library.com's catalog, and each skill's SKILL.md from GitHub.

Outbound calls go only to the hosts in SKILLS_ALLOWED_HOSTS, and every URL
still passes the SSRF guard's IP check. Nothing about the user is sent: the
catalog is one public JSON file; a skill is three public GitHub reads (the
repo's default branch, its file tree, the raw SKILL.md). Every body is read
with a byte cap, a redirect is followed only to another allow-listed host, and
scripts are never fetched.

The catalog (about 6,000 entries) is held in memory, sorted by installs, with
a word -> entries index: a search intersects the posting sets of the query's
words instead of scanning the list. It is reloaded when older than a day — it
only decides what an Admin can find to import; what is imported is re-read
and vetted from the skill's own repository.

Complexity: loading O(catalog size); a search O(sum of the query words'
posting sets) plus sorting the matches; an import O(repo tree size).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
import structlog

from sila.adapters.skills.base import (
    SKILL_KEY_PATTERN,
    SOURCE_PATTERN,
    CatalogEntry,
    SkillSourceError,
)
from sila.core.security.ssrf_guard import validate_external_url

log = structlog.get_logger(__name__)

_CATALOG_TTL_S = 24 * 3600
_MAX_REDIRECTS = 3
_REDIRECTS = frozenset({301, 302, 307, 308})
# The catalog is ~6,000 entries today; a file this large is not the catalog.
_MAX_ENTRIES = 50_000
_WORD = re.compile(r"[a-z0-9]+")


# Where a repository usually keeps a skill (tried first, as raw content).
_USUAL_PATHS = ("skills/{key}/SKILL.md", "{key}/SKILL.md")


class _NotFoundError(SkillSourceError):
    """The upstream has no such file (HTTP 404)."""


class _Catalog:
    """The loaded catalog: entries by installs, a word index, a key lookup."""

    def __init__(self, entries: list[CatalogEntry]) -> None:
        self.entries = sorted(entries, key=lambda e: -e.installs)
        self.loaded_at = time.monotonic()
        self.by_key = {(e.source, e.skill_key): e for e in self.entries}
        self.index: dict[str, set[int]] = {}
        for position, entry in enumerate(self.entries):
            for word in _words(f"{entry.name} {entry.skill_key} {entry.description}"):
                self.index.setdefault(word, set()).add(position)

    def search(self, query: str) -> list[CatalogEntry]:
        words = _words(query)
        if not words:
            return self.entries
        postings = sorted((self.index.get(w, set()) for w in words), key=len)
        matches = set.intersection(*postings) if postings else set()
        return [self.entries[i] for i in sorted(matches)]


class SkillsLibraryAdapter:
    """SkillSource over skills-library.com + GitHub."""

    # Shared by every adapter in the process. Two admins racing on a cold cache
    # may load it twice — harmless, and cheaper than a lock bound to one loop.
    _catalog: _Catalog | None = None

    def __init__(
        self,
        *,
        catalog_url: str,
        github_api_url: str,
        raw_url: str,
        allowed_hosts: Iterable[str],
        github_token: str = "",
        catalog_max_bytes: int = 15_000_000,
        skill_md_max_bytes: int = 64_000,
        timeout_s: float = 20.0,
    ) -> None:
        self._catalog_url = catalog_url
        self._api = github_api_url.rstrip("/")
        self._raw = raw_url.rstrip("/")
        self._hosts = frozenset(h.strip().lower() for h in allowed_hosts if h.strip())
        self._token = github_token
        self._catalog_max = catalog_max_bytes
        self._skill_max = skill_md_max_bytes
        self._timeout = timeout_s

    # --- SkillSource -------------------------------------------------------

    async def search(self, query: str, *, page: int, size: int) -> tuple[list[CatalogEntry], int]:
        matches = (await self._loaded()).search(query)
        start = (page - 1) * size
        return matches[start : start + size], len(matches)

    async def entry(self, source: str, skill_key: str) -> CatalogEntry | None:
        return (await self._loaded()).by_key.get((source, skill_key))

    async def fetch_skill_md(self, entry: CatalogEntry) -> str:
        """The skill's SKILL.md: the usual places first, straight from raw content
        (one request, and not counted against GitHub's API limit of 60 an hour
        without a token); the repository's file tree only when both miss."""
        _check_ids(entry.source, entry.skill_key)
        key = entry.skill_key.rsplit(":", 1)[-1]
        for path in _USUAL_PATHS:
            url = f"{self._raw}/{entry.source}/HEAD/{quote(path.format(key=key))}"
            try:
                return (await self._get(url, self._skill_max)).decode("utf-8", errors="replace")
            except _NotFoundError:
                continue
        return await self._from_tree(entry)

    async def _from_tree(self, entry: CatalogEntry) -> str:
        """Find SKILL.md through the GitHub API (default branch + recursive tree)."""
        repo = await self._json(f"{self._api}/repos/{entry.source}", self._skill_max * 4)
        branch = str(repo.get("default_branch") or "")
        if not SKILL_KEY_PATTERN.fullmatch(branch):
            raise SkillSourceError(f"{entry.source}: no usable default branch")
        tree = await self._json(
            f"{self._api}/repos/{entry.source}/git/trees/{quote(branch)}?recursive=1",
            self._catalog_max,
        )
        path = _skill_path(tree.get("tree") or [], entry.skill_key)
        if path is None:
            raise SkillSourceError(f"{entry.source}: no SKILL.md for '{entry.skill_key}'")
        url = f"{self._raw}/{entry.source}/{quote(branch)}/{quote(path)}"
        return (await self._get(url, self._skill_max)).decode("utf-8", errors="replace")

    # --- internals ---------------------------------------------------------

    async def _loaded(self) -> _Catalog:
        cls = type(self)
        catalog = cls._catalog
        if catalog is not None and time.monotonic() - catalog.loaded_at < _CATALOG_TTL_S:
            return catalog
        raw = await self._json(self._catalog_url, self._catalog_max)
        catalog = _Catalog(list(_entries(raw)))
        cls._catalog = catalog
        log.info("skills.catalog_loaded", entries=len(catalog.entries))
        return catalog

    async def _json(self, url: str, max_bytes: int) -> Any:
        try:
            return json.loads(await self._get(url, max_bytes))
        except ValueError as exc:
            raise SkillSourceError(f"{urlsplit(url).hostname} returned invalid JSON") from exc

    async def _get(self, url: str, max_bytes: int) -> bytes:
        """GET an allow-listed URL with a byte cap.

        A redirect is followed only to another allow-listed host, re-checked by
        the SSRF guard, at most _MAX_REDIRECTS times — GitHub answers a renamed
        repository with a 301 to its new address.
        """
        for _ in range(_MAX_REDIRECTS + 1):
            status, location, body = await self._get_once(url, max_bytes)
            if status == 200:
                return body
            if status in _REDIRECTS and location:
                url = urljoin(url, location)
                continue
            if status == 404:
                raise _NotFoundError(f"{urlsplit(url).hostname} answered HTTP 404")
            raise SkillSourceError(f"{urlsplit(url).hostname} answered HTTP {status}")
        raise SkillSourceError(f"too many redirects fetching {urlsplit(url).hostname}")

    async def _get_once(self, url: str, max_bytes: int) -> tuple[int, str, bytes]:
        """One request: (status, redirect target, body) — no redirect followed."""
        host = (urlsplit(url).hostname or "").lower()
        if host not in self._hosts:
            raise SkillSourceError(f"{host or url!r} is not an allowed skills host")
        try:
            await validate_external_url(url)
        except ValueError as exc:
            raise SkillSourceError(str(exc)) from exc
        headers = {"Accept": "application/json, text/plain", "User-Agent": "sila-skills"}
        if self._token and host == urlsplit(self._api).hostname:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client,
                client.stream("GET", url, headers=headers) as resp,
            ):
                if resp.status_code != 200:
                    return resp.status_code, resp.headers.get("location", ""), b""
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise SkillSourceError(f"{host}: response larger than {max_bytes:,} bytes")
                    chunks.append(chunk)
                return 200, "", b"".join(chunks)
        except httpx.HTTPError as exc:
            raise SkillSourceError(f"{host} could not be reached: {exc}") from exc


def _check_ids(source: str, skill_key: str) -> None:
    if not SOURCE_PATTERN.fullmatch(source) or not SKILL_KEY_PATTERN.fullmatch(skill_key):
        raise SkillSourceError("not a valid skill reference")
    if ".." in source or ".." in skill_key:
        raise SkillSourceError("not a valid skill reference")


def _skill_path(tree: list[dict[str, Any]], skill_key: str) -> str | None:
    """The SKILL.md whose folder is the skill's key (shortest path first)."""
    key = skill_key.lower().rsplit(":", 1)[-1]
    candidates = []
    for node in tree:
        path = str(node.get("path") or "")
        parts = path.lower().split("/")
        if node.get("type") == "blob" and parts[-1] == "skill.md" and parts[-2:-1] == [key]:
            candidates.append(path)
    return min(candidates, key=len) if candidates else None


def _entries(raw: Any) -> Iterable[CatalogEntry]:
    """Catalog records → entries; anything malformed is skipped, not guessed."""
    if not isinstance(raw, list):
        raise SkillSourceError("the catalog is not a list of skills")
    if len(raw) > _MAX_ENTRIES:
        raise SkillSourceError(f"the catalog lists {len(raw):,} skills; refusing to load it")
    for item in raw:
        if not isinstance(item, dict):
            continue
        source, key = str(item.get("source") or ""), str(item.get("skillId") or "")
        if not SOURCE_PATTERN.fullmatch(source) or not SKILL_KEY_PATTERN.fullmatch(key):
            continue
        content = item.get("contentType") or {}
        deps = item.get("dependencies") or []
        yield CatalogEntry(
            source=source,
            skill_key=key,
            name=str(item.get("name") or key)[:120],
            description=str(item.get("description") or "")[:1000],
            repo_url=f"https://github.com/{source}",
            installs=int(item.get("installs") or 0),
            license=(str(item["license"])[:120] if item.get("license") else None),
            has_scripts=bool(isinstance(content, dict) and content.get("hasScripts")),
            dependencies=[
                {"type": str(d.get("type", ""))[:20], "name": str(d.get("name", ""))[:60]}
                for d in deps[:20]
                if isinstance(d, dict)
            ],
        )


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))
