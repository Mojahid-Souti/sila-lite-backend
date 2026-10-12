"""PDD Template — Phase 1: ingest. Normalize, roster, pseudonyms, chunks.

Everything here is pure code — the roster comes from transcript STRUCTURE
(speaker-line patterns), never from a model, so a speaker cannot be dropped
or invented. The normalized text produced here is the canonical `source`
every evidence span references for the rest of the pipeline.

Pseudonymization (the PDPL control): real names/emails map to stable tokens
([Contact Name 1], [Email 1]) applied at the EXPORT boundary; the mapping
lives only in session state and is never written into the document (spec
Appendix C: "mapping held by document owner").

Complexity: O(n) over the transcript for every function; the chunker is a
single pass with line-boundary splits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CHUNK_CHARS = 8_000
CHUNK_OVERLAP = 800

# A speaker line: optional [timestamp], a short capitalized name (≤ 4 words),
# an optional (role), then a colon and speech. Deliberately conservative —
# prose sentences with colons ("Note: …") are excluded by the name shape.
_SPEAKER_RE = re.compile(
    r"^\s*(?:\[[^\]]{1,16}\]\s*)?"  # [00:12:03]
    r"(?P<name>[A-Z][\w.'-]*(?:\s+[A-Z0-9][\w.'-]*){0,3})"  # "Speaker 3" counts
    r"(?:\s*\((?P<role>[^)]{1,60})\))?"
    r"\s*:\s+\S",
    re.MULTILINE,
)
_NON_SPEAKERS = frozenset(
    {
        "note",
        "notes",
        "agenda",
        "action",
        "actions",
        "subject",
        "date",
        "re",
        "fwd",
        "summary",
        "attendees",
        "topic",
        "warning",
        "important",
        "nb",
        "ps",
    }
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def normalize(raw: str) -> str:
    """The canonical source text: BOM stripped, line endings unified, NBSP →
    space. Conservative on purpose — every downstream span points HERE."""
    text = raw.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return text.replace(" ", " ")


@dataclass
class Speaker:
    name: str
    role: str = ""
    mentions: int = 0
    first_offset: int = 0


def build_roster(source: str) -> list[Speaker]:
    """Every speaker in the source, from line structure alone (zero model
    involvement). First-appearance order; the first non-empty role wins."""
    roster: dict[str, Speaker] = {}
    for m in _SPEAKER_RE.finditer(source):
        name = m.group("name").strip()
        if name.lower() in _NON_SPEAKERS:
            continue
        key = name.lower()
        entry = roster.get(key)
        if entry is None:
            roster[key] = Speaker(
                name=name,
                role=(m.group("role") or "").strip(),
                mentions=1,
                first_offset=m.start("name"),
            )
        else:
            entry.mentions += 1
            if not entry.role and m.group("role"):
                entry.role = m.group("role").strip()
    return sorted(roster.values(), key=lambda s: s.first_offset)


@dataclass
class Pseudonymizer:
    """Stable identity tokens: the SAME person is the same pseudonym in the
    stakeholder table, the RACI matrix, and the sign-off page (spec rule 5)."""

    names: dict[str, str] = field(default_factory=dict)  # lowercase name → token
    emails: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_roster(cls, roster: list[Speaker]) -> Pseudonymizer:
        return cls(names={s.name.lower(): f"[Contact Name {i + 1}]" for i, s in enumerate(roster)})

    def pseudonym_for(self, name: str) -> str:
        return self.names.get(name.strip().lower(), name)

    def apply(self, text: str) -> str:
        """Redact a rendered string at the export boundary: emails FIRST (a
        name inside an address must not break the email pattern), then every
        roster name, case-insensitive and word-bounded."""

        def _email(m: re.Match[str]) -> str:
            addr = m.group(0).lower()
            if addr not in self.emails:
                self.emails[addr] = f"[Email {len(self.emails) + 1}]"
            return self.emails[addr]

        out = _EMAIL_RE.sub(_email, text)
        for name in sorted(self.names, key=len, reverse=True):
            out = re.sub(rf"\b{re.escape(name)}\b", self.names[name], out, flags=re.IGNORECASE)
        return out

    @property
    def entity_count(self) -> int:
        return len(self.names) + len(self.emails)


@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    text: str


def chunk_source(source: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[Chunk]:
    """Overlapping line-boundary chunks with absolute offsets, so chunk-local
    spans re-base to the full source (start + local). Lossless by
    construction: every character of the source is inside at least one chunk
    and chunk.text == source[start:end], always."""
    if len(source) <= size:
        return [Chunk(0, len(source), source)]
    chunks: list[Chunk] = []
    start = 0
    while start < len(source):
        end = min(start + size, len(source))
        if end < len(source):
            newline = source.rfind("\n", start + size // 2, end)
            if newline != -1:
                end = newline + 1
        chunks.append(Chunk(start, end, source[start:end]))
        if end >= len(source):
            break
        start = max(end - overlap, start + 1)
    return chunks
