"""Generated-file downloads and previews — capability URLs for artifacts SILA
builds in chat.

No JWT on purpose: the link is clicked as a plain <a> from the chat, so the
unguessable UUIDv4 id (~122 bits) IS the authorization — the same class as
Grafana public-share links. Rows expire after 24h; expired/unknown ids 404.
The preview routes inherit exactly that model: same id, same expiry, no new
way to reach a file you could not already download.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.repositories.generated_files import GeneratedFileRepository
from sila.db.session import get_db
from sila.services.office_convert import office_to_pdf

router = APIRouter(tags=["files"])

# A preview is for reading, not for loading a dataset into the browser. Past
# this the pane says so and the user downloads the real thing.
_TEXT_PREVIEW_LIMIT = 200_000

_PDF = "application/pdf"

# Office formats a browser cannot render but Gotenberg can turn into a PDF.
_CONVERTIBLE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/msword",
    "application/vnd.ms-powerpoint",
    "application/vnd.ms-excel",
)

_SHEET_SUFFIXES = (".csv", ".tsv")
_MARKDOWN_SUFFIXES = (".md", ".markdown")
_TEXT_SUFFIXES = (".txt", ".json", ".yaml", ".yml", ".log", ".xml")


def _is_textual(content_type: str, filename: str) -> bool:
    low = filename.lower()
    return (
        content_type.startswith("text/")
        or content_type in {"application/json", "application/x-yaml"}
        or low.endswith(_SHEET_SUFFIXES + _MARKDOWN_SUFFIXES + _TEXT_SUFFIXES)
    )


def _preview_mode(content_type: str, filename: str) -> str:
    """How the pane should render this file.

    'sheet' → grid, 'markdown' → formatted, 'text' → monospace, 'pdf' → embedded
    viewer, 'none' → the pane offers the download and says why.
    """
    low = filename.lower()
    if low.endswith(_SHEET_SUFFIXES):
        return "sheet"
    if low.endswith(_MARKDOWN_SUFFIXES):
        return "markdown"
    if _is_textual(content_type, filename):
        return "text"
    if content_type == _PDF or content_type in _CONVERTIBLE:
        return "pdf"
    return "none"


async def _load(file_id: uuid.UUID, db: AsyncSession) -> Any:
    row = await GeneratedFileRepository(db).get_live(file_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return row


@router.get("/files/{file_id}")
async def download_generated_file(
    file_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    row = await _load(file_id, db)
    safe_name = row.filename.replace('"', "")
    return Response(
        content=row.file_bytes,
        media_type=row.content_type,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.get("/files/{file_id}/preview")
async def preview_generated_file(
    file_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Metadata the preview pane needs, plus the text itself when it is text.

    Text comes back in this JSON rather than as a second fetch of the raw file:
    the raw route is `attachment`, so reading it in the browser would mean
    serving generated content inline — see `view_generated_file`.
    """
    row = await _load(file_id, db)
    mode = _preview_mode(row.content_type, row.filename)
    payload: dict[str, Any] = {
        "filename": row.filename,
        "content_type": row.content_type,
        "size_bytes": len(row.file_bytes),
        "mode": mode,
        "truncated": False,
    }
    if mode in {"sheet", "markdown", "text"}:
        blob = row.file_bytes[:_TEXT_PREVIEW_LIMIT]
        payload["truncated"] = len(row.file_bytes) > _TEXT_PREVIEW_LIMIT
        # Generated files are ours and UTF-8, but a stray byte must degrade to a
        # readable preview rather than a 500.
        payload["text"] = blob.decode("utf-8", errors="replace")
    return payload


@router.get("/files/{file_id}/view")
async def view_generated_file(
    file_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """The file as an inline PDF, for the pane's embedded viewer.

    PDF ONLY, deliberately. This is the one route that sets `inline`, and
    anything a browser renders inline runs on the API's origin — so serving a
    generated file under its own content type here would turn "SILA wrote an
    HTML/SVG file" into script execution against this origin. A PDF renders in
    the sandboxed viewer instead, and every other type is refused rather than
    passed through.
    """
    row = await _load(file_id, db)
    if row.content_type == _PDF:
        data: bytes | None = row.file_bytes
    elif row.content_type in _CONVERTIBLE:
        data = await office_to_pdf(row.file_bytes, row.filename)
    else:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="No inline preview for this file type",
        )
    if data is None:
        # Conversion is best-effort (Gotenberg down); the pane falls back to the
        # download rather than showing a broken viewer.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Preview conversion unavailable",
        )
    safe_name = f"{row.filename.rsplit('.', 1)[0]}.pdf".replace('"', "")
    return Response(
        content=data,
        media_type=_PDF,
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            # Belt and braces alongside the PDF-only rule above.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; object-src 'self'",
        },
    )
