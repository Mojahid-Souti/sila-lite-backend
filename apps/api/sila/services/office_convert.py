"""Office → PDF, via the Gotenberg sidecar already in the stack.

Gotenberg was wired only for deck thumbnails; the same LibreOffice endpoint
converts any Office format, which is what lets SILA hand back a PDF at all — the
tool registry had no PDF output before this, so "give me a PDF" quietly produced
a .docx or nothing.

Conversion is best-effort by design: a PDF is a SECOND rendering of a document
that already exists and is already attached, so Gotenberg being down must cost
the user the PDF, never the document.

Complexity: one HTTP round-trip, O(size) over the file.
"""

from __future__ import annotations

import httpx
import structlog

from sila.core.config import settings

log = structlog.get_logger(__name__)

# LibreOffice conversion is CPU-bound and a large deck is not quick; the caller
# is already inside a tool call the user is waiting on, so this is a ceiling
# rather than a target.
_CONVERT_TIMEOUT_S = 90.0


async def office_to_pdf(data: bytes, filename: str) -> bytes | None:
    """Convert an Office file to PDF. Returns None when conversion is unavailable.

    `filename` matters: Gotenberg picks its converter from the extension, so
    passing "report.docx" and "deck.pptx" is what routes each correctly.
    """
    if not data:
        return None
    try:
        async with httpx.AsyncClient(timeout=_CONVERT_TIMEOUT_S) as client:
            resp = await client.post(
                f"{settings.gotenberg_url.rstrip('/')}/forms/libreoffice/convert",
                files={"files": (filename, data, "application/octet-stream")},
            )
    except httpx.HTTPError as exc:
        log.warning("office_convert.unreachable", error=type(exc).__name__, file=filename)
        return None
    if resp.status_code >= 400:
        log.warning("office_convert.failed", status=resp.status_code, file=filename)
        return None
    return resp.content
