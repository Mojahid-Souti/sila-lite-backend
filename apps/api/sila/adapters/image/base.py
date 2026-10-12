"""Image-generation adapter interface — Rule 5 swap point.

The slide tool consumes this Protocol. Today it's the local FLUX.1-schnell
container sharing the GPU with the brain; a production box with a second
GPU (or a managed service) implements the same method and nothing upstream
changes. Failures NEVER raise to the caller — a missing image degrades to a
styled placeholder, so a deck always builds.
"""

from __future__ import annotations

from typing import Protocol


class ImageGateway(Protocol):
    async def generate_images(
        self, prompts: list[str], *, width: int = 1024, height: int = 576
    ) -> list[bytes | None]:
        """Generate one PNG per prompt (same order). An entry is None when
        generation is unavailable or failed for that prompt — the caller
        renders a placeholder. Implementations own any GPU orchestration
        (e.g. unloading the brain first, freeing the model after)."""
        ...
