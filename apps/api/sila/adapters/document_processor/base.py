from typing import Protocol


class DocumentProcessorProtocol(Protocol):
    async def extract_pages(self, content: bytes, file_type: str) -> list[str]:
        """Return list of page strings. OCR applied when no text layer detected."""
        ...
