"""Analysis Engine v2 — the typed session state every analysis tool reads/writes.

Phase 0 ships the skeleton that later stages fill in: the dataset handle
(DuckDB-backed, never a dataframe in RAM), the stage marker, and the
ProvenanceLedger (every emitted number → its lineage). Objective, cleaning
recipe, findings, and the semantic layer land in Phases 1–4 — the fields
exist now so tools can start writing to one object instead of loose keys.

Sessions live in process memory keyed by the SILA chat session id (a WS
connection's lifetime); nothing here is user-mutable over the wire.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Stage = Literal["objective", "collect", "clean", "analyze", "interpret", "communicate", "monitor"]


@dataclass
class DatasetRef:
    """A handle to a FULL dataset on disk (DuckDB reads it lazily) — the
    replacement for head-truncated attachment text."""

    id: str
    path: str
    filename: str
    rows: int | None = None


@dataclass
class ProvenanceEntry:
    """One emitted artifact's lineage: which source, which operations."""

    artifact: str  # chart title / finding id / stat name
    source: str  # filename or dataset id
    operations: list[str] = field(default_factory=list)  # ordered, human-readable
    generated_at: float = field(default_factory=time.time)


@dataclass
class ProvenanceLedger:
    entries: list[ProvenanceEntry] = field(default_factory=list)

    def record(self, artifact: str, source: str, operations: list[str]) -> None:
        self.entries.append(
            ProvenanceEntry(artifact=artifact, source=source, operations=operations)
        )

    def explain(self, artifact: str) -> ProvenanceEntry | None:
        """Newest lineage for an artifact — powers 'why is this number X?'."""
        for e in reversed(self.entries):
            if e.artifact == artifact:
                return e
        return None


@dataclass
class AnalysisSession:
    session_id: str
    stage: Stage = "collect"
    dataset: DatasetRef | None = None
    # Phase 1+: objective, cleaning_recipe, semantic_layer, plan, findings, insights.
    objective: dict[str, Any] | None = None
    provenance: ProvenanceLedger = field(default_factory=ProvenanceLedger)
    created_at: float = field(default_factory=time.time)


_SESSIONS: dict[str, AnalysisSession] = {}
_TTL_S = 6 * 3600  # a chat session's working memory, not a store


def get_session(session_id: str | uuid.UUID) -> AnalysisSession:
    """Fetch-or-create the analysis state for a chat session. O(1); expired
    entries are swept opportunistically."""
    now = time.time()
    for key in [k for k, s in _SESSIONS.items() if now - s.created_at > _TTL_S]:
        _SESSIONS.pop(key, None)
    key = str(session_id)
    if key not in _SESSIONS:
        _SESSIONS[key] = AnalysisSession(session_id=key)
    return _SESSIONS[key]
