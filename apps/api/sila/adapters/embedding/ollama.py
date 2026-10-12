"""Embedding adapter that calls Ollama's /api/embeddings endpoint.

Swap point (Rule 5): replace with OpenAIEmbeddingAdapter or SentenceTransformerAdapter
without changing any caller.

Complexity: one HTTP round-trip per batch — O(b) where b = batch_size (32 by default).
"""

import logging

import httpx

logger = logging.getLogger(__name__)


class OllamaEmbeddingAdapter:
    def __init__(self, base_url: str, model: str, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Call Ollama once per text (Ollama /api/embeddings is per-string, not batched).

        We issue concurrent requests for the batch — O(b) HTTP calls, pipelined.
        """
        import asyncio

        async def _embed_one(text: str) -> list[float]:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/api/embeddings",
                    json={"model": self._model, "prompt": text},
                )
                resp.raise_for_status()
                embedding: list[float] = resp.json()["embedding"]
                return embedding

        return list(await asyncio.gather(*[_embed_one(t) for t in texts]))
