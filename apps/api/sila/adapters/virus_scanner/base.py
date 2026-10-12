from typing import Protocol


class VirusScannerProtocol(Protocol):
    async def scan(self, content: bytes) -> bool:
        """Return True if file is clean, False if infected. Raises on scan failure."""
        ...
