"""PDD Template — Phase 3: derivation & consistency. Pure code, no model.

Everything here is `derived` in the A.11 sense — computed from grounded
fields, deterministic, and re-runnable:

  - §2.4 Applications ← every system named anywhere in the extracted JSON
    (AS-IS steps, TO-BE steps, data inputs); unknown details say
    "To confirm", they are never invented;
  - §4.1 FRs ← at least one per TO-BE step, phrased with the doc type's
    mandatory verb ("BOT should…" / "Macro should…");
  - §4.3 + Appendix B traceability ← each FR points at the speaker, quote,
    and source LINE of the step that produced it;
  - §7 RACI ← roster × automated steps with EXACTLY one A per activity
    (validated, not hoped for);
  - §1.4.3 Stakeholders ← the roster when the transcript never listed them;
  - §6.4 Glossary ← acronyms whose definition appears in the source as a
    parenthetical ("Purchase Order (PO)") — an acronym without a source
    definition is a Phase-4 question, never a made-up entry;
  - '#' columns renumber 1..n so cross-references stay consistent.

Complexity: O(items + cells) single pass; the acronym scan is O(source).
"""

from __future__ import annotations

import re
from typing import Any

from sila.services.pdd.extract import ExtractedItem, ExtractionResult
from sila.services.pdd.ingest import Speaker
from sila.services.pdd.registry import DOC_TYPE_VERB, flat_sections

_TO_CONFIRM = "To confirm"
_GENERIC_SYSTEMS = frozenset({"", "-", "—", "n/a", "none", "manual", "email"})
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,5}\b")
# Everyday abbreviations and currencies never become clarifying questions.
_ACRONYM_STOPLIST = frozenset(
    {
        "IT",
        "HR",
        "AM",
        "PM",
        "CEO",
        "CFO",
        "COO",
        "OK",
        "NB",
        "PS",
        "OMR",
        "USD",
        "EUR",
        "GBP",
        "AED",
        "SAR",
        "RPA",
        "PDD",
        "VBA",
    }
)
_MAX_ACRONYM_QUESTIONS = 5


def _columns(section_id: str) -> tuple[str, ...]:
    return next(s.columns for s in flat_sections("macro") if s.id == section_id)


def _cell(row: list[str], section_id: str, column: str) -> str:
    cols = _columns(section_id)
    idx = cols.index(column)
    return str(row[idx]).strip() if idx < len(row) else ""


def _line_number(source: str, offset: int) -> int:
    return source.count("\n", 0, max(0, offset)) + 1


def _items_for(result: ExtractionResult, section_id: str) -> list[ExtractedItem]:
    return [i for i in result.items if i.section_id == section_id]


def _derive_applications(content: dict[str, Any], result: ExtractionResult) -> None:
    """§2.4 ← the union of systems named anywhere. Existing extracted rows are
    kept; missing systems are appended with explicit To-confirm cells."""
    known = {
        _cell(r, "2.4", "Application").lower() for r in (content.get("2.4") or {}).get("rows", [])
    }
    mentioned: list[str] = []
    for section_id, column in (("2.3", "System"), ("3.3", "System"), ("6.1", "Source system")):
        for item in _items_for(result, section_id):
            system = _cell(list(item.payload), section_id, column)
            if system.lower() not in _GENERIC_SYSTEMS and system.lower() not in known:
                known.add(system.lower())
                mentioned.append(system)
    if mentioned:
        rows = content.setdefault("2.4", {"rows": []})["rows"]
        rows.extend(["", app, _TO_CONFIRM, _TO_CONFIRM, _TO_CONFIRM] for app in mentioned)


def _derive_requirements(
    content: dict[str, Any], result: ExtractionResult, *, source: str, doc_type: str
) -> None:
    """§4.1 (≥1 FR per TO-BE step) + §4.3 + Appendix B traceability."""
    steps = _items_for(result, "3.3")
    if not steps:
        return
    verb = DOC_TYPE_VERB[doc_type]
    frs, traces, appendix = [], [], []
    for n, item in enumerate(steps, start=1):
        row = list(item.payload)
        step = _cell(row, "3.3", "Step")
        action = _cell(row, "3.3", "BOT/Macro action") or step
        rules = _cell(row, "3.3", "Decision rules")
        fr_id = f"FR-{n:02d}"
        description = f"{verb} {action[0].lower()}{action[1:]}" if action else f"{verb} {step}"
        frs.append([f"3.3 / {n}", fr_id, description, rules, "High", "Mandatory"])
        line = _line_number(source, item.evidence.span[0])
        traces.append([fr_id, f"{item.evidence.speaker} — line {line}", item.evidence.speaker])
        appendix.append([fr_id, item.evidence.speaker, f"line {line}: “{item.evidence.quote}”"])
    content["4.1"] = {"rows": frs}
    content["4.3"] = {"rows": traces}
    content["B"] = {"rows": appendix}


def _derive_raci(content: dict[str, Any], result: ExtractionResult, roster: list[Speaker]) -> None:
    """§7 ← activities (3.3 step names) × roster, EXACTLY one A per activity:
    the speaker who described the step is Accountable (their account grounds
    it); the busiest other speaker is Responsible; everyone else is Informed."""
    steps = _items_for(result, "3.3")
    if not steps or not roster:
        return
    names = [s.name for s in roster]
    by_mentions = sorted(roster, key=lambda s: -s.mentions)
    rows = []
    for item in steps:
        activity = _cell(list(item.payload), "3.3", "Step")
        accountable = item.evidence.speaker
        if accountable.lower() not in {n.lower() for n in names}:
            accountable = names[0]
        responsible = next(
            (s.name for s in by_mentions if s.name.lower() != accountable.lower()), accountable
        )
        cells = [activity]
        for name in names:
            if name.lower() == accountable.lower():
                cells.append("A")
            elif name.lower() == responsible.lower():
                cells.append("R")
            else:
                cells.append("I")
        rows.append(cells)
    content["7"] = {
        "note": "Figure: RACI matrix — generated from §3.3 activities × §1.4.3 roles.",
        "columns": ["Activity", *names],
        "rows": rows,
    }


def validate_raci(raci: dict[str, Any]) -> None:
    """The spec's hard rule: exactly one A per activity. Raises on violation."""
    for row in raci.get("rows", []):
        a_count = sum(1 for cell in row[1:] if str(cell).strip().upper() == "A")
        if a_count != 1:
            raise ValueError(f"RACI activity '{row[0]}' has {a_count} Accountable cells (need 1)")


def _derive_stakeholders(content: dict[str, Any], roster: list[Speaker]) -> None:
    if (content.get("1.4.3") or {}).get("rows"):
        return
    content["1.4.3"] = {
        "rows": [
            ["", s.name, s.role or _TO_CONFIRM, s.role or _TO_CONFIRM, "", "Held by document owner"]
            for s in roster
        ]
    }


def _derive_glossary(content: dict[str, Any], source: str) -> list[str]:
    """§6.4 from parenthetical definitions ONLY. Returns the acronyms that
    appear without a source definition — Phase 4 asks about those."""
    acronyms: dict[str, str] = {}
    undefined: list[str] = []
    existing = {_cell(r, "6.4", "Term").upper() for r in (content.get("6.4") or {}).get("rows", [])}
    for acronym in sorted(set(_ACRONYM_RE.findall(source))):
        if acronym in existing or acronym in ("A", "I", "R", "C", "S"):
            continue
        if acronym in _ACRONYM_STOPLIST:
            continue
        # A definition is a Title-Cased word sequence right before "(ACRO)" —
        # a lazy [A-Z].{3,60} grabbed "I export the Purchase Order" (live bug).
        defined = re.search(
            rf"([A-Z][a-z]+(?:\s+[A-Za-z][a-z]+){{0,5}})\s*\({re.escape(acronym)}\)", source
        )
        reverse = re.search(rf"{re.escape(acronym)}\s*\(([^)]{{3,60}})\)", source)
        if defined:
            acronyms[acronym] = defined.group(1).strip()
        elif reverse:
            acronyms[acronym] = reverse.group(1).strip()
        elif len(undefined) < _MAX_ACRONYM_QUESTIONS:
            undefined.append(acronym)
    if acronyms:
        rows = content.setdefault("6.4", {"rows": []})["rows"]
        rows.extend([term, definition] for term, definition in sorted(acronyms.items()))
    return undefined


def _renumber(content: dict[str, Any]) -> None:
    """'#' columns count 1..n after every merge/append — cross-references
    (FR '3.3 / n') rely on this order."""
    for section in flat_sections("macro"):
        payload = content.get(section.id)
        if section.kind == "table" and section.columns[:1] == ("#",) and isinstance(payload, dict):
            for i, row in enumerate(payload.get("rows", []), start=1):
                if row:
                    row[0] = str(i)


def derive(
    result: ExtractionResult,
    *,
    source: str,
    roster: list[Speaker],
    doc_type: str,
) -> tuple[dict[str, Any], list[str]]:
    """Grounded extraction → the full derived content dict (renderer shape)
    plus the list of undefined acronyms for the Phase-4 gap register."""
    content = result.content_for_render()
    _derive_applications(content, result)
    _derive_requirements(content, result, source=source, doc_type=doc_type)
    _derive_raci(content, result, roster)
    if "7" in content:
        validate_raci(content["7"])
    _derive_stakeholders(content, roster)
    undefined = _derive_glossary(content, source)
    _renumber(content)
    return content, undefined
