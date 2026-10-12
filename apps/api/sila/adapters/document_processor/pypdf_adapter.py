"""Document text extraction via pypdf (PDF), python-docx (DOCX), and stdlib (TXT/MD/HTML).

Swap point (Rule 5): replace this adapter with UnstructuredAdapter or DoclingAdapter
to get richer layout extraction without changing any caller.
"""

import io
import logging
import shutil

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_ALLOWED_TYPES = {"pdf", "docx", "txt", "md", "html"}

# Resolve OCR availability ONCE at import, not per page. An image-heavy PDF
# (e.g. a slide deck) has no text layer on every page, so the old code tried
# to decode an image and shell out to tesseract for each one. When tesseract
# isn't installed every attempt fails — but the per-page image decode is
# CPU-bound and holds the GIL, starving the asyncio event loop and making the
# whole API unresponsive. If OCR can't work, skip it entirely.
_OCR_AVAILABLE = shutil.which("tesseract") is not None
if not _OCR_AVAILABLE:
    logger.info("Tesseract not found — OCR fallback disabled; image-only PDF pages yield no text")


class PypdfAdapter:
    """Extract text from documents. OCR fallback via pytesseract when a PDF has no text layer."""

    async def extract_pages(self, content: bytes, file_type: str) -> list[str]:
        """O(n) over pages/sections. Returns list of page/section strings."""
        if file_type not in _ALLOWED_TYPES:
            raise ValueError(f"Unsupported file type: {file_type}")

        if file_type == "pdf":
            return await self._extract_pdf(content)
        if file_type == "docx":
            return self._extract_docx(content)
        if file_type == "html":
            return self._extract_html(content)
        # txt / md — treat as single page
        return [content.decode("utf-8", errors="replace")]

    async def _extract_pdf(self, content: bytes) -> list[str]:
        import asyncio

        return await asyncio.get_running_loop().run_in_executor(
            None, self._extract_pdf_sync, content
        )

    def _extract_pdf_sync(self, content: bytes) -> list[str]:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if not text.strip():
                # OCR fallback for image-only page
                text = self._ocr_page(page)
            pages.append(text)
        return pages

    def _ocr_page(self, page: object) -> str:
        """Tesseract OCR on a pypdf page image. Returns empty string if pytesseract unavailable."""
        # Skip the image-decode + OCR shell-out entirely when tesseract is
        # absent — that work would just fail anyway, but pegs CPU/GIL per page.
        if not _OCR_AVAILABLE:
            return ""
        try:
            import pytesseract
            from PIL import Image
            from pypdf import PdfWriter

            writer = PdfWriter()
            writer.add_page(page)  # type: ignore[arg-type]
            buf = io.BytesIO()
            writer.write(buf)
            buf.seek(0)

            # Render page as image via pypdf's page.images (if available)
            images = getattr(page, "images", [])
            if not images:
                return ""
            img = Image.open(io.BytesIO(images[0].data))
            return str(pytesseract.image_to_string(img, lang="eng"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR unavailable: %s", exc)
            return ""

    def _extract_docx(self, content: bytes) -> list[str]:
        from docx import Document

        doc = Document(io.BytesIO(content))
        # Group paragraphs into ~page-sized sections (every 30 paragraphs)
        pages: list[str] = []
        section: list[str] = []
        for i, para in enumerate(doc.paragraphs):
            section.append(para.text)
            if (i + 1) % 30 == 0:
                pages.append("\n".join(section))
                section = []
        if section:
            pages.append("\n".join(section))
        return pages or [""]

    def _extract_html(self, content: bytes) -> list[str]:
        soup = BeautifulSoup(content, "html.parser")
        return [soup.get_text(separator="\n", strip=True)]
