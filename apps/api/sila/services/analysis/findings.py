"""Analysis Engine Phase 3 — the typed Finding and its guardrails.

A Finding is the ONLY thing an analysis tool may hand the model: ground-truth
statement slots plus a phrasing rendered HERE, in code. The guardrails are
structural, not persona discipline:

  - n below threshold → confidence "low", and the word "significant" cannot
    appear (the template that contains it is unreachable for low confidence).
  - every correlation/driver finding carries an association≠causation caveat
    that is appended at construction and travels with the finding.
  - the Simpson check compares the headline direction against within-segment
    directions and emits a `contradiction` caveat on reversal.

Complexity: O(1) per finding; Simpson check O(segments).
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SMALL_N = 30  # below this, no claim may be called significant
_P_HIGH = 0.01
_P_MEDIUM = 0.05

CAUSATION_CAVEAT = "this is association, not causation"
_id_counter = itertools.count(1)

Kind = Literal[
    "trend", "difference", "correlation", "anomaly", "driver", "segment", "concentration"
]


@dataclass
class Finding:
    id: str
    kind: str
    statement_slots: dict[str, Any]  # ground truth ONLY — the model quotes these
    effect_size: float
    p_value: float | None
    n: int
    confidence: str  # "high" | "medium" | "low"
    caveats: list[str] = field(default_factory=list)
    evidence: dict[str, Any] | None = None  # a ChartSpec in the existing format
    objective_relevance: float = 0.5
    phrasing: str = ""  # rendered in code, never by the model

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def confidence_for(n: int, p_adjusted: float | None) -> str:
    """The deterministic confidence policy. Small n is a hard ceiling: no
    p-value, however tiny, lifts a 7-row claim above 'low'."""
    if n < SMALL_N:
        return "low"
    if p_adjusted is None:
        return "medium"  # descriptive: solid numbers, no inferential test
    if p_adjusted < _P_HIGH:
        return "high"
    if p_adjusted < _P_MEDIUM:
        return "medium"
    return "low"


def significance_note(confidence: str, p_adjusted: float | None, tests_run: int) -> str:
    """The ONLY path to the word 'significant' — unreachable for low
    confidence, so a small-n or weak result can never carry it."""
    if confidence == "low" or p_adjusted is None or p_adjusted >= _P_MEDIUM:
        return ""
    corrected = f", corrected for {tests_run} tests" if tests_run > 1 else ""
    return f" (statistically significant: adjusted p={p_adjusted:.3g}{corrected})"


def make_finding(
    kind: str,
    slots: dict[str, Any],
    phrasing: str,
    *,
    effect_size: float = 0.0,
    p_value: float | None = None,
    p_adjusted: float | None = None,
    n: int = 0,
    tests_run: int = 1,
    caveats: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
    objective: dict[str, Any] | None = None,
) -> Finding:
    """Assemble a Finding with the guardrails applied: confidence from the
    policy, the significance note appended only when legitimate, the small-n
    caveat added automatically, causation caveat forced for correlation-family
    kinds."""
    confidence = confidence_for(n, p_adjusted)
    all_caveats = list(caveats or [])
    if n < SMALL_N:
        all_caveats.append(f"n={n} is too small to claim statistical significance")
    if kind in ("correlation", "driver") and CAUSATION_CAVEAT not in all_caveats:
        all_caveats.append(CAUSATION_CAVEAT)
    if tests_run > 1:
        slots = {**slots, "tests_run": tests_run}
    slots = {**slots, "n": n}
    if p_adjusted is not None:
        # Same 3-sig-fig rendering the significance note uses, so the quoted
        # value and the slot value are the identical number (tiny p included).
        slots = {**slots, "p_adjusted": float(f"{p_adjusted:.3g}")}
    text = phrasing + significance_note(confidence, p_adjusted, tests_run)
    return Finding(
        id=f"f-{kind}-{next(_id_counter)}",
        kind=kind,
        statement_slots=slots,
        effect_size=round(float(effect_size), 4),
        p_value=None if p_value is None else round(float(p_value), 6),
        n=n,
        confidence=confidence,
        caveats=all_caveats,
        evidence=evidence,
        objective_relevance=_relevance(slots, objective),
        phrasing=text,
    )


def _relevance(slots: dict[str, Any], objective: dict[str, Any] | None) -> float:
    """Objective as the ranking signal: a finding about a metric the user
    named scores 1.0; one mentioned in their question 0.8; the rest 0.5."""
    if not objective:
        return 0.5
    mentioned = {str(m).lower() for m in objective.get("metrics") or []}
    question = str(objective.get("question", "")).lower()
    for key in ("metric", "target", "column", "by"):
        value = str(slots.get(key, "")).lower()
        if value and value in mentioned:
            return 1.0
        if value and value in question:
            return 0.8
    return 0.5


def simpson_check(
    overall_diff: float,
    per_segment_diffs: dict[str, tuple[float, int]],
    total_n: int,
    min_coverage: float = 0.2,
) -> str | None:
    """The contradiction guardrail: if the headline direction between two
    groups REVERSES inside a segment covering ≥ min_coverage of rows, return
    the caveat text (the caller must attach and relay it)."""
    if overall_diff == 0 or total_n <= 0:
        return None
    reversed_in = [
        seg
        for seg, (diff, seg_n) in per_segment_diffs.items()
        if seg_n / total_n >= min_coverage and diff != 0 and (diff > 0) != (overall_diff > 0)
    ]
    if not reversed_in:
        return None
    where = ", ".join(f"'{s}'" for s in sorted(reversed_in))
    return (
        f"contradiction: the overall direction REVERSES within {where} — "
        "Simpson's paradox risk; the headline may be an artifact of group mix"
    )
