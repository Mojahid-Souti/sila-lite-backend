"""SkillSource — where open-source Agent Skills come from (Rule 5).

`SkillsLibraryAdapter` reads skills-library.com's public catalog and each
skill's SKILL.md from its GitHub repository. A private registry (Omantel's own
reviewed skills) or skills.sh directly would be another implementation of this
Protocol; the service layer depends on nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# "owner/repo" and a skill's key within it, as the upstream catalog writes them.
SOURCE_PATTERN = re.compile(r"[\w.-]{1,100}/[\w.-]{1,100}")
SKILL_KEY_PATTERN = re.compile(r"[\w.:-]{1,120}")


class SkillSourceError(RuntimeError):
    """The upstream could not be read, or refused what it returned."""


@dataclass(frozen=True)
class CatalogEntry:
    """One skill as the upstream catalog lists it (metadata; no instructions)."""

    source: str
    skill_key: str
    name: str
    description: str
    repo_url: str
    installs: int = 0
    license: str | None = None
    has_scripts: bool = False
    dependencies: list[dict[str, Any]] = field(default_factory=list)


class SkillSource(Protocol):
    async def search(self, query: str, *, page: int, size: int) -> tuple[list[CatalogEntry], int]:
        """Catalog entries matching `query` (all when empty), most installed first."""
        ...

    async def entry(self, source: str, skill_key: str) -> CatalogEntry | None:
        """One catalog entry, or None when the catalog has no such skill."""
        ...

    async def fetch_skill_md(self, entry: CatalogEntry) -> str:
        """The skill's SKILL.md text. Raises SkillSourceError."""
        ...
