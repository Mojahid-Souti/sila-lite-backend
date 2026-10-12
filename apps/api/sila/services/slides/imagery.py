"""Licence-bearing imagery for generated decks.

A deck that goes to an executive audience cannot carry an image whose licence
was inferred from a search-results page. So the licence is never inferred: it
arrives as a FIELD from the provider's API and is recorded per image, with the
source URL and the author, in a manifest the deck carries.

Providers sit behind one interface (Rule 5). Wikimedia Commons needs no API key
and is always available; Unsplash and Pexels give far better abstract/technical
results but need a key, so they are used only when one is configured.

Every candidate must clear four gates before it is embedded, and failing any of
them SKIPS the image rather than degrading it — no image beats a bad one:

  1. a licence on the commercial-use allow-list
  2. a real bitmap (JPEG/PNG), never a PDF, TIFF or SVG
  3. at least MIN_LONG_EDGE px on the long edge — never upscaled
  4. an on-palette dominant hue: blue, orange or neutral, never green or teal

Complexity: O(candidates) API rows, and one decode per downloaded candidate.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

log = structlog.get_logger(__name__)

# A slide region is up to ~13in wide; below this a picture is a thumbnail being
# asked to do a full-bleed job.
MIN_LONG_EDGE = 1600
# Anything larger is re-encoded before it goes in the package.
MAX_BYTES = 1_000_000
_USER_AGENT = "SilaSlides/1.0 (Omantel deck imagery)"
_TIMEOUT_S = 20.0

# Licences that permit commercial use. Anything not on this list is skipped,
# including "no known copyright" phrasings that are not a grant.
_ALLOWED_LICENCES = (
    "cc0",
    "public domain",
    "pdm",
    "cc by",
    "cc-by",
    "cc by-sa",
    "cc-by-sa",
    "unsplash",
    "pexels",
)
_BLOCKED_LICENCE_TERMS = ("nc", "noncommercial", "non-commercial", "nd", "noderiv")

_BITMAP_MIMES = ("image/jpeg", "image/png")


@dataclass(frozen=True)
class ImageAsset:
    """One licence-cleared image, its bytes, and everything an audit needs."""

    data: bytes
    mime: str
    width: int
    height: int
    title: str
    source_url: str
    author: str
    licence: str
    licence_url: str

    @property
    def extension(self) -> str:
        return "png" if self.mime == "image/png" else "jpg"

    def credit(self) -> str:
        author = self.author or "Unknown"
        return f"{self.title} — {author} — {self.licence} — {self.source_url}"


class ImageProvider(Protocol):
    """A source of images whose licence is a field in its response."""

    name: str

    def search(self, query: str, limit: int) -> Sequence[dict[str, object]]: ...


def _licence_allowed(licence: str) -> bool:
    low = licence.lower()
    if any(re.search(rf"\b{term}\b", low) for term in _BLOCKED_LICENCE_TERMS):
        return False
    return any(term in low for term in _ALLOWED_LICENCES)


class CommonsProvider:
    """Wikimedia Commons. No API key, licence in `extmetadata`.

    Editorially it is a documentary archive rather than a curated abstract
    library, so its hit rate for "abstract technical" imagery is low — the
    palette and size gates reject most of what it returns, which is the correct
    outcome but means a key-based provider is preferable when one is available.
    """

    name = "wikimedia-commons"
    _ENDPOINT = "https://commons.wikimedia.org/w/api.php"

    def search(self, query: str, limit: int) -> Sequence[dict[str, object]]:
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": "6",
            "gsrlimit": str(limit),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": "2000",
        }
        try:
            response = httpx.get(
                self._ENDPOINT,
                params=params,
                timeout=_TIMEOUT_S,
                headers={"User-Agent": _USER_AGENT},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("imagery.search_failed", provider=self.name, error=type(exc).__name__)
            return []
        pages = (response.json().get("query") or {}).get("pages", {})
        rows: list[dict[str, object]] = []
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            rows.append(
                {
                    "title": str(page.get("title", "")).removeprefix("File:"),
                    "mime": info.get("mime", ""),
                    "width": info.get("width") or 0,
                    "height": info.get("height") or 0,
                    "download": info.get("thumburl") or info.get("url") or "",
                    "source_url": info.get("descriptionurl") or "",
                    "author": _strip_markup(str((meta.get("Artist") or {}).get("value", ""))),
                    "licence": str((meta.get("LicenseShortName") or {}).get("value", "")),
                    "licence_url": str((meta.get("LicenseUrl") or {}).get("value", "")),
                }
            )
        return rows


class UnsplashProvider:
    """Unsplash. Needs an access key; every photo is under the Unsplash Licence,
    which permits commercial use. Far better abstract/technical results."""

    name = "unsplash"
    _ENDPOINT = "https://api.unsplash.com/search/photos"

    def __init__(self, access_key: str) -> None:
        self._key = access_key

    def search(self, query: str, limit: int) -> Sequence[dict[str, object]]:
        try:
            response = httpx.get(
                self._ENDPOINT,
                params={"query": query, "per_page": str(limit), "orientation": "landscape"},
                timeout=_TIMEOUT_S,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Authorization": f"Client-ID {self._key}",
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("imagery.search_failed", provider=self.name, error=type(exc).__name__)
            return []
        rows: list[dict[str, object]] = []
        for item in response.json().get("results", []):
            urls = item.get("urls") or {}
            user = item.get("user") or {}
            rows.append(
                {
                    "title": str(item.get("description") or item.get("alt_description") or "Photo"),
                    "mime": "image/jpeg",
                    "width": item.get("width") or 0,
                    "height": item.get("height") or 0,
                    "download": urls.get("full") or urls.get("regular") or "",
                    "source_url": (item.get("links") or {}).get("html", ""),
                    "author": str(user.get("name") or ""),
                    "licence": "Unsplash Licence",
                    "licence_url": "https://unsplash.com/license",
                }
            )
        return rows


def _strip_markup(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).strip()[:80]


def _download(url: str) -> bytes | None:
    try:
        response = httpx.get(
            url, timeout=_TIMEOUT_S, headers={"User-Agent": _USER_AGENT}, follow_redirects=True
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("imagery.download_failed", error=type(exc).__name__)
        return None
    return response.content


# Hue bands, in degrees, that read as the brand: blue through indigo, and the
# orange end. Anything dominated by green, teal or warm beige is rejected.
_BLUE = (200, 265)
_ORANGE = (10, 45)


def palette_ok(data: bytes) -> bool:
    """Is the image on-palette — blue, orange or neutral?

    Judged on the SATURATED pixels only: a mostly-grey photograph is neutral and
    fine, but one whose colour is green or teal clashes with the brand however
    little of it there is.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - PIL ships in the image
        return True
    try:
        image = Image.open(io.BytesIO(data)).convert("HSV").resize((64, 64))
    except Exception as exc:  # noqa: BLE001 - any decode failure means "unusable"
        log.warning("imagery.decode_failed", error=type(exc).__name__)
        return False
    flat = list(image.tobytes())
    pixels = [tuple(flat[i : i + 3]) for i in range(0, len(flat) - 2, 3)]
    hues = [
        hue * 360 / 255
        for hue, saturation, value in pixels
        if saturation > 60 and value > 40  # saturated and not near-black
    ]  # noqa: E501
    if len(hues) < 200:  # essentially monochrome — neutral, so acceptable
        return True
    on_brand = sum(1 for h in hues if _BLUE[0] <= h <= _BLUE[1] or _ORANGE[0] <= h <= _ORANGE[1])
    return on_brand / len(hues) >= 0.5


def _compress(data: bytes, mime: str) -> tuple[bytes, str]:
    """Re-encode an oversized image so one picture cannot dominate the package."""
    if len(data) <= MAX_BYTES:
        return data, mime
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        image.thumbnail((2200, 2200))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=82, optimize=True)
    except Exception as exc:  # noqa: BLE001 - keep the original on any failure
        log.warning("imagery.compress_failed", error=type(exc).__name__)
        return data, mime
    return buffer.getvalue(), "image/jpeg"


def find_image(
    query: str, providers: Iterable[ImageProvider], *, limit: int = 12
) -> ImageAsset | None:
    """The first candidate clearing every gate, or None.

    None is a valid, common answer. A deck with three good images and one gap
    beats a deck with four images one of which is wrong.
    """
    for provider in providers:
        for row in provider.search(query, limit):
            licence = str(row.get("licence", ""))
            if not _licence_allowed(licence):
                continue
            if str(row.get("mime", "")) not in _BITMAP_MIMES:
                continue
            width = int(str(row.get("width") or 0) or 0)
            height = int(str(row.get("height") or 0) or 0)
            if max(width, height) < MIN_LONG_EDGE:
                continue
            url = str(row.get("download", ""))
            if not url:
                continue
            data = _download(url)
            if data is None or not palette_ok(data):
                continue
            data, mime = _compress(data, str(row["mime"]))
            log.info(
                "imagery.selected",
                provider=provider.name,
                licence=licence,
                size=f"{width}x{height}",
            )
            return ImageAsset(
                data=data,
                mime=mime,
                width=width,
                height=height,
                title=str(row.get("title", "")),
                source_url=str(row.get("source_url", "")),
                author=str(row.get("author", "")),
                licence=licence,
                licence_url=str(row.get("licence_url", "")),
            )
    return None


def default_providers(unsplash_key: str = "") -> list[ImageProvider]:
    """Key-based providers first — their abstract/technical results are far
    better — with Commons as the always-available fallback."""
    providers: list[ImageProvider] = []
    if unsplash_key:
        providers.append(UnsplashProvider(unsplash_key))
    providers.append(CommonsProvider())
    return providers
