"""SearchAdapter Protocol — interface for full-text search backends.

The concrete implementation (MeilisearchAdapter) is swappable at the adapter layer
per Rule 5. The service layer only depends on this protocol.
"""

from __future__ import annotations

from typing import Protocol


class SearchAdapter(Protocol):
    async def configure_index(self) -> None:
        """Idempotent index setup (filterable/sortable attributes). Call at startup."""
        ...

    async def index_agent(self, agent: dict[str, object]) -> None:
        """Upsert agent document into the search index. O(s) where s = doc size."""
        ...

    async def delete_agent(self, agent_id: str) -> None:
        """Remove agent from the search index. O(1)."""
        ...

    async def search(
        self,
        query: str,
        *,
        category_ids: list[str],
        page: int,
        size: int,
    ) -> tuple[list[dict[str, object]], int]:
        """Full-text search over published agents.

        Returns (hits, total_count). O(log n) via Meilisearch inverted index.
        n = number of indexed published agents.
        """
        ...
