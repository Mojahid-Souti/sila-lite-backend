"""Classification policy loader.

Reads `infra/policies/classification.yaml` and exposes typed accessors.

Why a loader (vs. hard-coded constants):
  - Compliance team can edit YAML without touching code (after ADR sign-off)
  - Same policy file is shipped to ops in any environment
  - Adding a new classification level (e.g. `top_secret`) is one YAML edit
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml


@dataclass(frozen=True)
class LevelPolicy:
    description: str
    require_reviewer: bool
    allowed_tools: tuple[str, ...]
    denied_tools: tuple[str, ...]
    allowed_kb_scopes: frozenset[str]

    def tool_allowed(self, tool_name: str) -> bool:
        """Deny overrides allow. Returns True if `tool_name` may be granted."""
        if any(fnmatch.fnmatch(tool_name, deny) for deny in self.denied_tools):
            return False
        return any(fnmatch.fnmatch(tool_name, allow) for allow in self.allowed_tools)


@dataclass(frozen=True)
class ClassificationPolicy:
    levels: dict[str, LevelPolicy]
    auto_approve_block_severity: str
    hard_block_severity: str

    def level(self, name: str) -> LevelPolicy:
        try:
            return self.levels[name]
        except KeyError as e:
            raise ValueError(f"Unknown classification level: {name!r}") from e

    def validate_tools(self, classification: str, tool_names: list[str]) -> list[str]:
        """Return the subset of tools that violate the classification policy.
        Empty list = all tools allowed. O(t × p) with both bounded small."""
        lvl = self.level(classification)
        return [t for t in tool_names if not lvl.tool_allowed(t)]


# Resolution order:
#   1. `path` argument (tests)
#   2. SILA_CLASSIFICATION_POLICY_PATH env var (deployed containers)
#   3. infra/policies/classification.yaml in the repo (host/dev)
REPO_POLICY_PATH: Final[Path] = (
    Path(__file__).resolve().parents[4] / "infra" / "policies" / "classification.yaml"
)


@lru_cache(maxsize=1)
def get_classification_policy(path: Path | None = None) -> ClassificationPolicy:
    """Cached loader. Pass `path` to override (used by tests)."""
    if path is not None:
        src = Path(path)
    else:
        env_path = os.environ.get("SILA_CLASSIFICATION_POLICY_PATH")
        src = Path(env_path) if env_path else REPO_POLICY_PATH
    raw = yaml.safe_load(src.read_text(encoding="utf-8"))

    levels: dict[str, LevelPolicy] = {}
    for name, cfg in raw.get("levels", {}).items():
        levels[name] = LevelPolicy(
            description=str(cfg.get("description", "")),
            require_reviewer=bool(cfg.get("require_reviewer", False)),
            allowed_tools=tuple(cfg.get("allowed_tools") or []),
            denied_tools=tuple(cfg.get("denied_tools") or []),
            allowed_kb_scopes=frozenset(cfg.get("allowed_kb_scopes") or []),
        )

    return ClassificationPolicy(
        levels=levels,
        auto_approve_block_severity=str(raw.get("auto_approve_block_severity", "high")),
        hard_block_severity=str(raw.get("hard_block_severity", "critical")),
    )
