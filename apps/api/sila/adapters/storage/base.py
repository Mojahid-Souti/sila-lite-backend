from typing import Protocol


class ObjectStoreProtocol(Protocol):
    async def put(self, path: str, content: bytes, content_type: str) -> str:
        """Store object; return canonical storage path."""
        ...

    async def get(self, path: str) -> bytes:
        """Download object bytes. Raises if the path does not exist."""
        ...

    async def delete(self, path: str) -> None:
        """Delete object. No-op if path does not exist."""
        ...

    async def get_signed_url(self, path: str, expires_in: int = 3600) -> str:
        """Return a time-limited download URL."""
        ...
