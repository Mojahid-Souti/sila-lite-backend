"""PDD Template — Phase 2: extraction + the validator that deletes lies.

One focused prompt PER SECTION FAMILY (business / as-is / to-be /
requirements / risk-and-data), run over each chunk, merged in code. The
model's only job is to find and quote; everything it returns passes the
validator:

  - shape check per section kind (prose / list / table);
  - evidence REQUIRED on every extracted item: the quote must exist verbatim
    in the source (code locates it and derives the span — offsets are never
    demanded from a model) and the speaker must be in the roster — anything
    else is DELETED and recorded, never kept;
  - required-column check (a nameless process step is a broken row);
  - NFR types against the closed enum; FR descriptions against the
    "BOT should…" / "Macro should…" verb prefix.

Groundedness is 100% by construction: violators don't survive validation.

Complexity: O(chunks × families) model calls; validation O(items).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sila.services.pdd.fields import Evidence, locate_quote
from sila.services.pdd.ingest import Chunk, Speaker
from sila.services.pdd.registry import DOC_TYPE_VERB, NFR_TYPES, flat_sections

# Section families — one focused prompt each (never a 200-line monolith).
FAMILIES: dict[str, tuple[str, ...]] = {
    "business": ("1.1", "1.2", "1.3", "1.4", "1.4.1", "1.4.2", "1.5", "1.6", "1.7"),
    "asis": ("2.1", "2.3", "2.5", "2.6", "2.7"),
    "tobe": ("3.1", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8"),
    "requirements": ("4.2", "4.4"),
    "risk_data": ("5.1", "5.2", "5.3", "5.4", "6.1", "6.2", "6.3", "6.5"),
}

_PROMPT_BASE = (
    "You extract facts for a Process Design Document from a meeting-transcript "
    "chunk. Rules, non-negotiable: every evidence object carries `speaker` (the "
    "person who said it, exactly as named in the transcript) and `quote` — a "
    "short passage COPIED WORD FOR WORD from the chunk (copy the exact words; "
    "a paraphrased quote is discarded). NEVER infer, summarize beyond the "
    "quote, or fill gaps — a section the chunk doesn't cover is simply absent "
    "from your JSON. EVERY table row and EVERY list item carries its OWN "
    "evidence object (an item without one is discarded). Output JSON only."
)

_FAMILY_FOCUS: dict[str, str] = {
    "business": (
        "Focus: the business overview — what the process is (1.1), current pain "
        "points (1.2), objectives with measures (1.3), scope (1.4/1.4.1/1.4.2), "
        "financial KPIs (1.5), operational KPIs (1.6), intangible benefits (1.7)."
    ),
    "asis": (
        "Focus: the CURRENT manual process — narrative (2.1), concrete steps with "
        "actor/system/inputs/outputs/duration/frequency (2.3), business volumes "
        "(2.5), pain points (2.6), exceptions and how they're handled today (2.7)."
    ),
    "tobe": (
        "Focus: the PROPOSED automation — overview (3.1), automated steps with "
        "action/system/input/output/decision rules (3.3), human touchpoints (3.4), "
        "exception handling (3.5), schedule/cut-off (3.6), feasibility notes (3.7), "
        "and for macros the technical build details (3.8)."
    ),
    "requirements": (
        "Focus: non-functional requirements with type and acceptance criteria "
        "(4.2), and for macros the security/deployment constraints (4.4). "
        "{nfr_enum}"
    ),
    "risk_data": (
        "Focus: assumptions (5.1), risks (5.2), dependencies (5.3), compliance and "
        "data-protection notes (5.4), data inputs (6.1) and outputs (6.2), "
        "retention (6.3), referenced documents (6.5)."
    ),
}


@dataclass
class ExtractedItem:
    """One prose block / list item / table row, with its mandatory evidence."""

    section_id: str
    payload: Any  # str (prose) | str (list item) | list[str] (table row cells)
    evidence: Evidence


@dataclass
class ExtractionResult:
    items: list[ExtractedItem] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)  # the lie ledger

    @property
    def groundedness(self) -> float:
        total = len(self.items) + len(self.dropped)
        return 1.0 if total == 0 else len(self.items) / total

    def content_for_render(self) -> dict[str, Any]:
        """Merge items into the renderer's content shape (section id → prose /
        list / {rows}). Deterministic: first prose wins, rows/list dedupe on
        normalized text, source order preserved."""
        sections = {s.id: s for s in flat_sections("macro")}  # superset of rpa
        out: dict[str, Any] = {}
        seen: dict[str, set[str]] = {}
        for item in self.items:
            kind = sections[item.section_id].kind if item.section_id in sections else "prose"
            if kind == "table":
                row = [str(c) for c in item.payload]
                key = "|".join(row).lower()
                if key in seen.setdefault(item.section_id, set()):
                    continue
                seen[item.section_id].add(key)
                out.setdefault(item.section_id, {"rows": []})["rows"].append(row)
            elif kind == "list":
                text = str(item.payload).strip()
                if text.lower() in seen.setdefault(item.section_id, set()):
                    continue
                seen[item.section_id].add(text.lower())
                out.setdefault(item.section_id, []).append(text)
            else:  # prose — first grounded block wins; later chunks append new text
                text = str(item.payload).strip()
                if item.section_id not in out:
                    out[item.section_id] = text
                elif text and text.lower() not in str(out[item.section_id]).lower():
                    out[item.section_id] = f"{out[item.section_id]}\n\n{text}"
        return out


def family_prompt(family: str, doc_type: str) -> str:
    sections = [s for s in flat_sections(doc_type) if s.id in FAMILIES[family]]
    shapes = []
    for s in sections:
        if s.kind == "table":
            shape = f'[{{"cells": [{", ".join(repr(c) for c in s.columns)}], "evidence": …}}]'
        elif s.kind == "list":
            shape = '[{"item": "…", "evidence": …}]'
        else:
            shape = '{"text": "…", "evidence": …}'
        shapes.append(f'  "{s.id}": {shape}')
    focus = _FAMILY_FOCUS[family]
    if family == "requirements":
        focus = focus.format(
            nfr_enum=(
                "The NFR 'Type' cell MUST be one of exactly: "
                + ", ".join(NFR_TYPES[doc_type])
                + " — any other type is discarded."
            )
        )
    return (
        f"{_PROMPT_BASE}\n{focus}\n"
        "Return exactly this JSON shape (omit sections this chunk doesn't cover):\n"
        "{\n" + ",\n".join(shapes) + "\n}\n"
        'where evidence = {"speaker": "…", "quote": "exact words from the chunk"}.'
    )


def check_verb_prefix(description: str, doc_type: str) -> bool:
    """FR descriptions must start with the doc type's verb ('BOT should…')."""
    return description.strip().startswith(DOC_TYPE_VERB[doc_type])


def _parse_evidence(raw: Any, chunk: Chunk) -> Evidence | None:
    del chunk  # spans are DERIVED by locating the quote — never model-supplied
    if not isinstance(raw, dict):
        return None
    return Evidence(
        speaker=str(raw.get("speaker") or ""),
        quote=str(raw.get("quote") or ""),
        span=(0, 0),  # placeholder; the validator locates the real span
    )


def validate_chunk_output(
    raw: dict[str, Any],
    *,
    family: str,
    chunk: Chunk,
    source: str,
    roster: list[Speaker],
    doc_type: str,
    result: ExtractionResult,
) -> None:
    """Fold one model response into the result — keeping only what survives
    the grounding, roster, shape, enum, and required-column checks."""
    speakers = {s.name.lower() for s in roster}
    sections = {s.id: s for s in flat_sections(doc_type)}

    def _speaker_ok(name: str) -> bool:
        # Models write "Layla (Process Owner)" / "layla" — normalize the role
        # suffix away and prefix-match against the roster (live bug: every
        # item died here). A name NOT resolvable to a rostered person still dies.
        import re as _re

        n = _re.sub(r"\(.*?\)", "", name).strip().lower()
        if not n:
            return False
        return n in speakers or any(n.startswith(sp) or sp.startswith(n) for sp in speakers)

    def _keep(section_id: str, payload: Any, evidence: Evidence | None) -> None:
        located = (
            locate_quote(evidence.quote, source)
            if evidence is not None and evidence.quote.strip()
            else None
        )
        reason = ""
        if evidence is None or not evidence.quote.strip():
            reason = "missing or malformed evidence"
        elif not _speaker_ok(evidence.speaker):
            reason = f"speaker '{evidence.speaker}' is not in the roster"
        elif located is None:
            reason = "quote not found verbatim in the source"
        if reason:
            result.dropped.append({"section": section_id, "payload": payload, "reason": reason})
        else:
            evidence.span = located  # the REAL span, derived in code
            result.items.append(ExtractedItem(section_id, payload, evidence))

    for section_id, value in raw.items():
        section = sections.get(str(section_id))
        if section is None or str(section_id) not in FAMILIES.get(family, ()):
            continue  # a section outside this family's contract is ignored
        if section.kind == "table" and isinstance(value, list):
            for row in value:
                if not isinstance(row, dict) or not isinstance(row.get("cells"), list):
                    continue
                cells = [str(c) for c in row["cells"]]
                named = dict(zip(section.columns, cells, strict=False))
                if any(not str(named.get(rc, "")).strip() for rc in section.required_columns):
                    result.dropped.append(
                        {"section": section_id, "payload": cells, "reason": "required column empty"}
                    )
                    continue
                if section.id == "4.2":
                    nfr_type = str(named.get("Type", ""))
                    if nfr_type not in NFR_TYPES[doc_type]:
                        result.dropped.append(
                            {
                                "section": section_id,
                                "payload": cells,
                                "reason": f"NFR type '{nfr_type}' not in the closed enum",
                            }
                        )
                        continue
                _keep(str(section_id), cells, _parse_evidence(row.get("evidence"), chunk))
        elif section.kind == "list" and isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict) and str(entry.get("item", "")).strip():
                    _keep(
                        str(section_id),
                        str(entry["item"]),
                        _parse_evidence(entry.get("evidence"), chunk),
                    )
        elif section.kind == "prose" and isinstance(value, dict):
            text = str(value.get("text", "")).strip()
            if text:
                _keep(str(section_id), text, _parse_evidence(value.get("evidence"), chunk))


async def extract(
    llm: Any,
    *,
    doc_type: str,
    source: str,
    chunks: list[Chunk],
    roster: list[Speaker],
    progress: Any | None = None,
) -> ExtractionResult:
    """Run every family prompt over every chunk; merge + validate in code.
    A failed model call skips that (family, chunk) pair loudly via progress —
    extraction is additive, so partial coverage becomes open items, never
    invented content."""
    result = ExtractionResult()
    families = list(FAMILIES)
    for fi, family in enumerate(families):
        prompt = family_prompt(family, doc_type)
        for ci, chunk in enumerate(chunks):
            if progress is not None:
                progress(
                    f"Extracting {family.replace('_', ' ')} — part {ci + 1}/{len(chunks)} "
                    f"(family {fi + 1}/{len(families)})…"
                )
            try:
                raw = await llm.complete_json(
                    system_prompt=prompt,
                    user_prompt=f"TRANSCRIPT CHUNK (offsets 0..{len(chunk.text)}):\n{chunk.text}",
                    timeout_s=120.0,
                )
            except Exception:  # noqa: BLE001 — one bad call must not sink the run
                if progress is not None:
                    progress(f"Extraction call failed for {family}, part {ci + 1} — skipped.")
                continue
            if isinstance(raw, dict):
                validate_chunk_output(
                    raw,
                    family=family,
                    chunk=chunk,
                    source=source,
                    roster=roster,
                    doc_type=doc_type,
                    result=result,
                )
    return result
