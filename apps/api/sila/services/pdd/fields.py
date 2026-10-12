"""PDD Template — the typed field model (spec A.11) and its validator.

Every value in the document is a Field with a provenance status:
  extracted     → MUST carry evidence (speaker + quote + span); a field that
                  cannot be grounded is DELETED by the validator, never kept.
  user_answered → came from the clarifying-question loop (Phase 4).
  derived       → computed by code from other fields (FRs, RACI, numbering).
  open          → renders in Section 8, never as invented body text.

The two governing metrics live here: coverage (% of required fields
populated) and groundedness (% of extracted fields with valid evidence —
enforced at 100% because the validator removes violators).

Complexity: O(fields) per validation pass.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Status = Literal["extracted", "user_answered", "derived", "open"]
Confidence = Literal["high", "medium", "low"]

_STATUSES = ("extracted", "user_answered", "derived", "open")
_CONFIDENCES = ("high", "medium", "low")


@dataclass
class Evidence:
    speaker: str
    quote: str
    span: tuple[int, int]  # [start, end) character offsets into the source

    def to_dict(self) -> dict[str, Any]:
        return {"speaker": self.speaker, "quote": self.quote, "span": list(self.span)}


@dataclass
class Field:
    value: Any
    status: str = "extracted"
    evidence: Evidence | None = None
    confidence: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = self.evidence.to_dict() if self.evidence else None
        return d


def field_from_dict(raw: dict[str, Any]) -> Field:
    """Parse an untrusted field dict (model output) into the typed model.
    Raises ValueError on a malformed shape — loud, never coerced silently."""
    status = str(raw.get("status") or "extracted")
    if status not in _STATUSES:
        raise ValueError(f"unknown field status '{status}'")
    confidence = str(raw.get("confidence") or "medium")
    if confidence not in _CONFIDENCES:
        raise ValueError(f"unknown confidence '{confidence}'")
    evidence = None
    ev = raw.get("evidence")
    if isinstance(ev, dict):
        span = ev.get("span") or []
        if len(span) == 2 and all(isinstance(x, int) for x in span) and 0 <= span[0] < span[1]:
            evidence = Evidence(
                speaker=str(ev.get("speaker") or ""),
                quote=str(ev.get("quote") or ""),
                span=(int(span[0]), int(span[1])),
            )
    return Field(value=raw.get("value"), status=status, evidence=evidence, confidence=confidence)


def locate_quote(quote: str, source: str) -> tuple[int, int] | None:
    """Find the quote's REAL span in the source — code does the locating, the
    model only quotes (models cannot count characters; demanding offsets from
    them deleted nearly everything in the live test). Exact match first, then
    case-insensitive, then whitespace-normalized. None = not in the source."""
    q = quote.strip()
    if not q:
        return None
    i = source.find(q)
    if i >= 0:
        return (i, i + len(q))
    low = source.lower().find(q.lower())
    if low >= 0:
        return (low, low + len(q))
    import re as _re

    tokens = [_re.escape(t) for t in q.split()]
    if tokens:
        m = _re.search(r"[\s]+".join(tokens), source, _re.IGNORECASE)
        if m:
            return (m.start(), m.end())
    # Last resort: the exact WORD sequence with any punctuation/whitespace
    # between (models normalize apostrophes and dashes when quoting).
    words = _re.findall(r"\w+", q)
    if len(words) >= 3:
        m = _re.search(r"\W+".join(map(_re.escape, words)), source, _re.IGNORECASE)
        if m:
            return (m.start(), m.end())
    return None


def evidence_grounded(evidence: Evidence | None, source: str) -> bool:
    """True when the quote really lives at (or near) its claimed span in the
    source. The quote must appear in the source at all; the span is validated
    with slack for whitespace normalization."""
    if evidence is None or not evidence.quote.strip():
        return False
    quote = evidence.quote.strip()
    if quote not in source:
        return False
    lo, hi = evidence.span
    window = source[max(0, lo - 200) : min(len(source), hi + 200)]
    return quote in window


@dataclass
class ValidationResult:
    kept: list[Field]
    dropped: list[Field]  # extracted-without-grounding — deleted, reported

    @property
    def groundedness(self) -> float:
        extracted = [f for f in self.kept if f.status == "extracted"]
        total = len(extracted) + len(self.dropped)
        return 1.0 if total == 0 else len(extracted) / total


def validate_fields(fields: list[Field], source: str) -> ValidationResult:
    """The spec's hard rule: `extracted` without valid evidence is DELETED.
    Everything kept is, by construction, 100% grounded."""
    kept: list[Field] = []
    dropped: list[Field] = []
    for f in fields:
        if f.status == "extracted" and not evidence_grounded(f.evidence, source):
            dropped.append(f)
        else:
            kept.append(f)
    return ValidationResult(kept=kept, dropped=dropped)


def coverage(populated: int, required: int) -> float:
    """Coverage over the required-field checklist (Phase 4 builds the list)."""
    return 1.0 if required == 0 else min(1.0, populated / required)
