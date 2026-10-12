"""HTTP adapter for the internal sila-image-gen (FLUX.1-schnell) container.

Owns the GPU hand-off: the brain and the painter can't both be resident on
the 12 GB card, so before generating a batch this unloads the brain (Ollama
`keep_alive=0`), generates every image, then releases the painter — the
brain reloads lazily on the next chat turn. Every failure is swallowed and
returned as None so the deck still builds with placeholders.
"""

from __future__ import annotations

import httpx
import structlog

log = structlog.get_logger(__name__)


class FluxImageAdapter:
    def __init__(
        self,
        base_url: str,
        ollama_url: str,
        brain_model: str,
        *,
        gen_timeout_s: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._ollama_url = ollama_url.rstrip("/")
        self._brain_model = brain_model
        self._gen_timeout_s = gen_timeout_s

    def _client(self, timeout_s: float) -> httpx.AsyncClient:
        # Test seam — tests inject httpx.MockTransport.
        return httpx.AsyncClient(timeout=timeout_s)

    async def _unload_brain(self) -> None:
        """Free the brain's VRAM so FLUX has room. Best-effort."""
        try:
            async with self._client(30.0) as client:
                await client.post(
                    f"{self._ollama_url}/api/generate",
                    json={"model": self._brain_model, "prompt": "", "keep_alive": 0},
                )
        except httpx.HTTPError as exc:
            log.warning("image.brain_unload_failed", error=str(exc))

    async def _release(self) -> None:
        try:
            async with self._client(30.0) as client:
                await client.post(f"{self._base_url}/release")
        except httpx.HTTPError as exc:
            log.warning("image.release_failed", error=str(exc))

    async def _one(
        self, client: httpx.AsyncClient, prompt: str, width: int, height: int
    ) -> bytes | None:
        try:
            resp = await client.post(
                f"{self._base_url}/generate",
                json={"prompt": prompt, "width": width, "height": height},
            )
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as exc:
            log.warning("image.generate_failed", error=str(exc), prompt=prompt[:60])
            return None

    async def generate_images(
        self, prompts: list[str], *, width: int = 1024, height: int = 576
    ) -> list[bytes | None]:
        if not prompts:
            return []
        await self._unload_brain()
        results: list[bytes | None] = []
        try:
            async with self._client(self._gen_timeout_s) as client:
                for prompt in prompts:
                    results.append(await self._one(client, prompt, width, height))
        finally:
            await self._release()  # return VRAM to the brain
        return results


class NullImageAdapter:
    """Used when image generation is disabled (no IMAGE_GEN_URL) — every
    prompt returns None, so the engine draws placeholders. Keeps the tool
    path identical whether or not the GPU feature is available."""

    async def generate_images(
        self, prompts: list[str], *, width: int = 1024, height: int = 576
    ) -> list[bytes | None]:
        return [None] * len(prompts)
