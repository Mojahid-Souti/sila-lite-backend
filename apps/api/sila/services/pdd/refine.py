"""PDD Template — Phase 6: the refine loop. Typed patches, never prose edits.

A conversational edit ("change the owner of the approval step to Sarah")
becomes a TYPED PATCH against the structured content, validated against the
registry, applied atomically, and re-rendered — the document is never edited
in place, which is why refinement used to be lossy.

Consistency is automatic: renaming a §3.3 step renames its RACI activity and
drops the TO-BE figure so the renderer regenerates it; adding/removing steps
extends/shrinks the RACI (one A per activity preserved and re-validated);
`set_raci` promotes exactly one Accountable and demotes the previous one.

Complexity: O(rows) per patch; every patch returns a plain-language change
line (ground truth for the chat reply).
"""

from __future__ import annotations

from typing import Any

from sila.services.pdd.derive import validate_raci
from sila.services.pdd.registry import Section, flat_sections

_META_KEYS = ("process_name", "version", "author", "department", "date", "status", "abstract")
_STEP_SECTIONS = {"2.3": "2.2", "3.3": "3.2"}  # table → the figure it feeds


class PatchError(ValueError):
    """A patch that doesn't fit the document — loud, nothing half-applied."""


def _section(section_id: str, doc_type: str) -> Section:
    section = next((s for s in flat_sections(doc_type) if s.id == section_id), None)
    if section is None:
        raise PatchError(f"no section '{section_id}' in a {doc_type} document")
    return section


def _rows(content: dict[str, Any], section_id: str) -> list[list[str]]:
    payload = content.get(section_id)
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise PatchError(f"section {section_id} has no table rows to edit")
    return payload["rows"]


def _column_index(section: Section, column: str) -> int:
    lowered = [c.lower() for c in section.columns]
    if column.lower() not in lowered:
        raise PatchError(
            f"section {section.id} has no column '{column}' (columns: {', '.join(section.columns)})"
        )
    return lowered.index(column.lower())


def _step_name_column(section: Section) -> int:
    for name in ("Step name", "Step"):
        if name in section.columns:
            return section.columns.index(name)
    return 1


def _sync_step_tables(content: dict[str, Any], section_id: str, doc_type: str) -> list[str]:
    """After any structural change to 2.3/3.3: drop the fed figure (the
    renderer regenerates it from rows) and, for 3.3, realign the RACI rows to
    the new activity list — letters preserved by position, new activities get
    a deterministic default (A on the first role), and the one-A rule is
    re-validated."""
    changed: list[str] = []
    figure_id = _STEP_SECTIONS[section_id]
    if figure_id in content:
        del content[figure_id]
        changed.append(f"figure §{figure_id} will regenerate from §{section_id}")
    if section_id != "3.3":
        return changed
    raci = content.get("7")
    if not isinstance(raci, dict) or not raci.get("columns"):
        return changed
    section = _section(section_id, doc_type)
    name_idx = _step_name_column(section)
    activities = [
        str(r[name_idx]).strip()
        for r in _rows(content, section_id)
        if len(r) > name_idx and str(r[name_idx]).strip()
    ]
    people = len(raci["columns"]) - 1
    old_rows = raci.get("rows") or []
    new_rows = []
    for i, activity in enumerate(activities):
        letters = (
            [str(c) for c in old_rows[i][1:]] if i < len(old_rows) else ["A", *["I"] * (people - 1)]
        )
        new_rows.append([activity, *letters[:people]])
    raci["rows"] = new_rows
    validate_raci(raci)
    changed.append("RACI matrix realigned to the updated steps")
    return changed


def apply_patch(
    content: dict[str, Any], meta: dict[str, Any], patch: dict[str, Any], *, doc_type: str
) -> list[str]:
    """Apply ONE typed patch; returns plain-language change lines. Raises
    PatchError on anything that doesn't fit — nothing is half-applied."""
    op = str(patch.get("op", ""))

    if op == "set_meta":
        key = str(patch.get("key", ""))
        if key not in _META_KEYS:
            raise PatchError(f"unknown meta key '{key}' (allowed: {', '.join(_META_KEYS)})")
        meta[key] = str(patch.get("value", ""))
        return [f"{key} → '{meta[key]}'"]

    if op == "set_raci":
        raci = content.get("7")
        if not isinstance(raci, dict) or not raci.get("rows"):
            raise PatchError("no RACI matrix to edit yet")
        activity = str(patch.get("activity", "")).strip().lower()
        person = str(patch.get("person", "")).strip().lower()
        letter = str(patch.get("letter", "")).strip().upper()
        if letter not in ("R", "A", "C", "I"):
            raise PatchError("letter must be one of R, A, C, I")
        columns = [str(c) for c in raci["columns"]]
        person_idx = next(
            (i for i, c in enumerate(columns) if i and c.strip().lower() == person), None
        )
        if person_idx is None:
            raise PatchError(
                f"'{patch.get('person')}' is not a RACI role (roles: {', '.join(columns[1:])})"
            )
        row = next((r for r in raci["rows"] if str(r[0]).strip().lower() == activity), None)
        if row is None:
            raise PatchError(f"no RACI activity '{patch.get('activity')}'")
        changed = []
        if letter == "A":
            for i in range(1, len(row)):  # demote the previous Accountable
                if str(row[i]).strip().upper() == "A" and i != person_idx:
                    row[i] = "C"
                    changed.append(f"{columns[i]} demoted A → C on '{row[0]}'")
        row[person_idx] = letter
        changed.append(f"{columns[person_idx]} → {letter} on '{row[0]}'")
        validate_raci(raci)
        return changed

    section_id = str(patch.get("section", ""))
    section = _section(section_id, doc_type)

    if op == "set_prose":
        if section.kind not in ("prose",):
            raise PatchError(f"section {section_id} is a {section.kind}, not prose")
        content[section_id] = str(patch.get("text", "")).strip()
        return [f"§{section_id} text replaced"]

    if op in ("add_item", "remove_item"):
        if section.kind != "list":
            raise PatchError(f"section {section_id} is a {section.kind}, not a list")
        items = content.setdefault(section_id, [])
        if op == "add_item":
            items.append(str(patch.get("text", "")).strip())
            return [f"§{section_id}: item added"]
        index = int(patch.get("row", 0)) - 1
        if not 0 <= index < len(items):
            raise PatchError(f"§{section_id} has {len(items)} items; no item {index + 1}")
        removed = items.pop(index)
        return [f"§{section_id}: removed '{removed}'"]

    if section.kind not in ("table", "image"):
        raise PatchError(f"'{op}' needs a table section; {section_id} is {section.kind}")

    if op == "set_cell":
        rows = _rows(content, section_id)
        index = int(patch.get("row", 0)) - 1
        if not 0 <= index < len(rows):
            raise PatchError(f"§{section_id} has {len(rows)} rows; no row {index + 1}")
        col = _column_index(section, str(patch.get("column", "")))
        old = str(rows[index][col]) if col < len(rows[index]) else ""
        while len(rows[index]) <= col:
            rows[index].append("")
        rows[index][col] = str(patch.get("value", ""))
        changed = [
            f"§{section_id} row {index + 1} {section.columns[col]}: '{old}' → '{rows[index][col]}'"
        ]
        if section_id in _STEP_SECTIONS and col == _step_name_column(section):
            changed += _sync_step_tables(content, section_id, doc_type)
        return changed

    if op == "add_row":
        cells = [str(c) for c in patch.get("cells") or []]
        if not any(c.strip() for c in cells):
            raise PatchError("add_row needs non-empty cells")
        padded = ([*cells, *[""] * len(section.columns)])[: len(section.columns)]
        for required in section.required_columns:
            if not padded[section.columns.index(required)].strip():
                raise PatchError(f"'{required}' is required for a §{section_id} row")
        content.setdefault(section_id, {"rows": []})["rows"].append(padded)
        changed = [f"§{section_id}: row added"]
        if section_id in _STEP_SECTIONS:
            changed += _sync_step_tables(content, section_id, doc_type)
        return changed

    if op == "remove_row":
        rows = _rows(content, section_id)
        index = int(patch.get("row", 0)) - 1
        if not 0 <= index < len(rows):
            raise PatchError(f"§{section_id} has {len(rows)} rows; no row {index + 1}")
        rows.pop(index)
        changed = [f"§{section_id}: row {index + 1} removed"]
        if section_id in _STEP_SECTIONS:
            changed += _sync_step_tables(content, section_id, doc_type)
        return changed

    raise PatchError(f"unknown patch op '{op}'")


def apply_patches(
    content: dict[str, Any],
    meta: dict[str, Any],
    patches: list[dict[str, Any]],
    *,
    doc_type: str,
) -> list[str]:
    """All-or-nothing: validate by applying to copies, then commit. A bad
    patch in the batch means NOTHING changes (and the error names it)."""
    import copy

    trial_content, trial_meta = copy.deepcopy(content), copy.deepcopy(meta)
    changes: list[str] = []
    for patch in patches:
        changes += apply_patch(trial_content, trial_meta, patch, doc_type=doc_type)
    content.clear()
    content.update(trial_content)
    meta.clear()
    meta.update(trial_meta)
    return changes
