"""Real slide-template previews — pptx → PDF (Gotenberg) → PNG thumbnails (poppler).

The template gallery hover-cycles through actual rendered slides (the Kimi
pattern). Rendering happens ONCE per template (at upload, or lazily backfilled)
and stays entirely on-box: Gotenberg is a headless LibreOffice container on the
private network; poppler rasterizes the pages inside the API container.

Best-effort by design: no renderer / a corrupt file → no thumbnails, never a
failed upload (the gallery falls back to generated covers).

Complexity: one conversion round-trip + O(pages) rasterization, once per file.
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
from pathlib import Path

import httpx
import structlog

from sila.core.config import settings

log = structlog.get_logger(__name__)

_MAX_PAGES = 6  # thumbnails shown in the gallery (hover cycles through them)
_THUMB_WIDTH = 384  # px — crisp on a ~300px card, still tiny on the wire
_CONVERT_TIMEOUT_S = 55.0  # Gotenberg's own --api-timeout is 60s


async def render_thumbnails(pptx_bytes: bytes) -> list[str]:
    """Preview slides for a TEMPLATE as PNG data-URIs. [] on any failure.

    Renders a SAMPLE-FILLED deck, not the raw file: (1) the gallery then shows
    what SILA's output looks like on that template (the Kimi pattern), and
    (2) LibreOffice chokes on some corporate originals' example slides (a
    false "password required" — live Omantel bug) while the filled deck,
    which strips them, converts cleanly."""
    from sila.services.connectors.pptx_template import Slide, TemplateError, fill_template

    try:
        sample = fill_template(
            pptx_bytes,
            "Your presentation",
            [
                Slide("Your presentation title", ("A subtitle or key message",)),
                Slide("Agenda", ("Where we are", "What changed", "What's next")),
                Slide(
                    "Key results",
                    ("First key point", "Second key point", "Supporting detail"),
                ),
                Slide("Next steps", ("Owner and date per action", "Risks to watch")),
            ],
        )
    except TemplateError:
        sample = pptx_bytes  # last resort: try the raw file
    pdf = await _to_pdf(sample)
    if pdf is None:
        return []
    return await asyncio.to_thread(_pdf_to_pngs, pdf)


async def _to_pdf(pptx_bytes: bytes) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=_CONVERT_TIMEOUT_S) as client:
            resp = await client.post(
                f"{settings.gotenberg_url.rstrip('/')}/forms/libreoffice/convert",
                files={"files": ("deck.pptx", pptx_bytes, "application/octet-stream")},
            )
        if resp.status_code >= 400:
            log.warning("pptx_thumbs.convert_failed", status=resp.status_code)
            return None
        return resp.content
    except httpx.HTTPError as exc:
        log.warning("pptx_thumbs.gotenberg_unreachable", error=type(exc).__name__)
        return None


def _pdf_to_pngs(pdf: bytes) -> list[str]:
    """pdftoppm the first pages to PNG data-URIs (sync — run in a thread)."""
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "deck.pdf"
        pdf_path.write_bytes(pdf)
        try:
            subprocess.run(
                [
                    "pdftoppm",
                    "-png",
                    "-f",
                    "1",
                    "-l",
                    str(_MAX_PAGES),
                    "-scale-to-x",
                    str(_THUMB_WIDTH),
                    "-scale-to-y",
                    "-1",
                    str(pdf_path),
                    str(Path(tmp) / "page"),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            log.warning("pptx_thumbs.pdftoppm_failed", error=type(exc).__name__)
            return []
        out: list[str] = []
        for png in sorted(Path(tmp).glob("page-*.png")):
            data = png.read_bytes()
            out.append("data:image/png;base64," + base64.b64encode(data).decode())
        return out
