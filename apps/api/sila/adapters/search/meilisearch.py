"""MeilisearchAdapter — SearchAdapter backed by Meilisearch.

Meilisearch Python SDK is synchronous; all calls are run in a thread pool
via asyncio.run_in_executor so they never block the event loop.

Index: "agents" — stores all published agents.
Filterable: status, category_ids.
Sortable:   updated_at.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from typing import Any

import meilisearch

logger = logging.getLogger(__name__)

_INDEX_NAME = "agents"


class MeilisearchAdapter:
    def __init__(self, url: str, master_key: str) -> None:
        self._client = meilisearch.Client(url, master_key)
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Internal helper: run sync call in thread pool
    # ------------------------------------------------------------------

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    # ------------------------------------------------------------------
    # Index configuration (idempotent)
    # ------------------------------------------------------------------

    async def configure_index(self) -> None:
        """Create index and set filterable/sortable attributes if not already done."""
        with contextlib.suppress(Exception):
            await self._run(self._client.create_index, _INDEX_NAME, {"primaryKey": "id"})

        index = self._client.index(_INDEX_NAME)
        try:
            await self._run(
                index.update_filterable_attributes,
                ["status", "category_ids"],
            )
            await self._run(index.update_sortable_attributes, ["updated_at"])
        except Exception as exc:
            logger.warning("Meilisearch index config failed: %s", exc)

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------

    async def index_agent(self, agent: dict[str, object]) -> None:
        """Upsert agent document. O(s) — async, fire-and-forget safe."""
        try:
            index = self._client.index(_INDEX_NAME)
            await self._run(index.add_documents, [agent])
        except Exception as exc:
            logger.warning("Meilisearch index_agent failed for %s: %s", agent.get("id"), exc)

    async def delete_agent(self, agent_id: str) -> None:
        """Remove agent from index. O(1)."""
        try:
            index = self._client.index(_INDEX_NAME)
            await self._run(index.delete_document, agent_id)
        except Exception as exc:
            logger.warning("Meilisearch delete_agent failed for %s: %s", agent_id, exc)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        category_ids: list[str],
        page: int,
        size: int,
    ) -> tuple[list[dict[str, object]], int]:
        """Search published agents.

        Filters: always status=published; optionally category_ids.
        Returns (hits, estimated_total). O(log n) via Meilisearch inverted index.
        """
        filters: list[str] = ["status = published"]
        if category_ids:
            cat_filter = " OR ".join(f"category_ids = {cid}" for cid in category_ids)
            filters.append(f"({cat_filter})")

        params: dict[str, object] = {
            "filter": " AND ".join(filters),
            "limit": size,
            "offset": (page - 1) * size,
        }

        try:
            index = self._client.index(_INDEX_NAME)
            result = await self._run(index.search, query, params)
            # Client-version drift: older meilisearch-python returns a plain
            # dict, newer returns a SearchResults object with attributes.
            if isinstance(result, dict):
                hits = list(result.get("hits", []))
                total = int(result.get("estimatedTotalHits") or 0)
            else:
                hits = list(result.hits)
                total = int(result.estimated_total_hits or 0)
            return hits, total
        except Exception as exc:
            logger.warning("Meilisearch search failed: %s", exc)
            return [], 0
