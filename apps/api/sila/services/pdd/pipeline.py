"""PDD Template — Phase 5: the export boundary.

One function owns the exit: the gap gate fires first (export is impossible
around it), open items merge into Section 8, Appendix C is derived from the
pseudonymizer, and ONLY THEN is every string redacted and rendered. Real
names never survive past this line; the mapping stays in session state.

Complexity: O(document strings) for the redaction walk.
"""

from __future__ import annotations

from typing import Any

from sila.services.pdd.gaps import GapRegister, assert_exportable, open_item_rows
from sila.services.pdd.ingest import Pseudonymizer
from sila.services.pdd.render import render_document


def _redact(value: Any, pseudo: Pseudonymizer) -> Any:
    if isinstance(value, str):
        return pseudo.apply(value)
    if isinstance(value, list):
        return [_redact(v, pseudo) for v in value]
    if isinstance(value, dict):
        return {k: _redact(v, pseudo) for k, v in value.items()}
    return value


def export_document(
    *,
    doc_type: str,
    meta: dict[str, Any],
    content: dict[str, Any],
    register: GapRegister,
    pseudonymizer: Pseudonymizer,
) -> bytes:
    """Gate → merge open items → Appendix C → redact → render. Raises
    ValueError (from the gate) while required gaps are unanswered+unwaived."""
    assert_exportable(register)
    merged: dict[str, Any] = dict(content)
    existing = list((merged.get("8") or {}).get("rows", []))
    merged["8"] = {"rows": [*existing, *open_item_rows(register, start_at=len(existing) + 1)]}
    # Redact FIRST (the walk also discovers emails), then write Appendix C
    # with the final entity count — the count itself is derived, never typed.
    redacted_meta = _redact(dict(meta), pseudonymizer)
    redacted = _redact(merged, pseudonymizer)
    redacted["C"] = (
        f"{pseudonymizer.entity_count} entities pseudonymized. Mapping held by document owner."
    )
    return render_document(doc_type, redacted_meta, redacted)
