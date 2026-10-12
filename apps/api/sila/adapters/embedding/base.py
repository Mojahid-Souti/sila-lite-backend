from typing import Protocol


class EmbeddingAdapter(Protocol):
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per text. Caller batches at configured size."""
        ...
