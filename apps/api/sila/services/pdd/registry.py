"""PDD Template — the section registry (spec Part A, loaded from sections.yaml).

The spine is data, not code: per-doc-type enablement, empty policies, and
table columns all live in the YAML next to this module. The invariant the
spec cares about — numbering is IDENTICAL across document types; macro-only
subsections are appended, nothing renumbers — is enforced by construction
(one spine, filtered) and pinned by a test.

Complexity: O(sections) once per process (cached).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

DocType = Literal["rpa", "macro"]
DOC_TYPE_LABEL: dict[str, str] = {
    "rpa": "Process Design Document",
    "macro": "Macro Automation Design Document",
}
# The verb the FR descriptions must start with, per doc type (validated in P2).
DOC_TYPE_VERB: dict[str, str] = {"rpa": "BOT should", "macro": "Macro should"}
# NFR types are a CLOSED enum per doc type (spec A.5).
NFR_TYPES: dict[str, tuple[str, ...]] = {
    "rpa": ("Performance", "Security", "Availability", "Scalability", "Maintainability"),
    "macro": ("Performance", "Compatibility", "Security", "Maintainability", "Usability"),
}


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    kind: str = "container"  # container | prose | list | table | image | cover | control | toc
    doc_types: tuple[str, ...] = ("rpa", "macro")
    empty_policy: str = "open_item"  # open_item | suppress
    columns: tuple[str, ...] = ()
    required_columns: tuple[str, ...] = ()
    note: str = ""
    source: str = ""  # image sections: which section's data generates the figure
    children: tuple[Section, ...] = field(default_factory=tuple)

    @property
    def numbered_title(self) -> str:
        return f"{self.id}. {self.title}" if self.id[0].isdigit() else self.title


def _parse(raw: dict[str, Any]) -> Section:
    children = tuple(_parse(c) for c in raw.get("children") or [])
    kind = str(raw.get("kind") or ("container" if children else "prose"))
    return Section(
        id=str(raw["id"]),
        title=str(raw["title"]),
        kind=kind,
        doc_types=tuple(raw.get("doc_types") or ("rpa", "macro")),
        empty_policy=str(raw.get("empty_policy") or "open_item"),
        columns=tuple(str(c) for c in raw.get("columns") or []),
        required_columns=tuple(str(c) for c in raw.get("required_columns") or []),
        note=str(raw.get("note") or ""),
        source=str(raw.get("source") or ""),
        children=children,
    )


@lru_cache(maxsize=1)
def _spine() -> dict[str, tuple[Section, ...]]:
    raw = yaml.safe_load((Path(__file__).parent / "sections.yaml").read_text(encoding="utf-8"))
    return {
        part: tuple(_parse(s) for s in raw[part])
        for part in ("front_matter", "sections", "appendices")
    }


def _filter(section: Section, doc_type: str) -> Section | None:
    if doc_type not in section.doc_types:
        return None
    kept = tuple(c for c in (_filter(child, doc_type) for child in section.children) if c)
    return Section(**{**section.__dict__, "children": kept})


def front_matter() -> tuple[Section, ...]:
    return _spine()["front_matter"]


def sections_for(doc_type: str) -> tuple[Section, ...]:
    """The numbered body sections enabled for this document type."""
    if doc_type not in DOC_TYPE_LABEL:
        raise ValueError(f"unknown document type '{doc_type}' (rpa | macro)")
    return tuple(s for s in (_filter(x, doc_type) for x in _spine()["sections"]) if s)


def appendices() -> tuple[Section, ...]:
    return _spine()["appendices"]


def flat_sections(doc_type: str) -> list[Section]:
    """Depth-first flattened body sections (numbering order)."""
    out: list[Section] = []

    def walk(section: Section) -> None:
        out.append(section)
        for child in section.children:
            walk(child)

    for top in sections_for(doc_type):
        walk(top)
    return out
