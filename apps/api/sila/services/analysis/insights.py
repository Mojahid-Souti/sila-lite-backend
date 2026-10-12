"""Analysis Engine Phase 4 — the insight engine: rank findings, narrate them
from statement slots ONLY, and build the executive summary.

The no-hallucination invariant is structural here: every piece of prose this
module generates is validated by `check_numbers_from_slots` at construction —
a numeric token that cannot be derived from the finding's statement_slots
raises before the text ever reaches the model. The so-what library is fixed
per finding kind and interpolates slot values, never free text.

score = effect_size_norm × coverage × objective_relevance × novelty
        × confidence_weight       (all factors in [0, 1])

Complexity: O(f log f) over f findings (sort); everything else O(1) per card.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?")
_CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}
_NOVELTY_DECAY = 0.6
_MAX_BULLETS = 5
_BULLET_CHARS = 160


# ---------------------------------------------------------------------------
# The number guard — prose may only contain numbers derivable from the slots
# ---------------------------------------------------------------------------


def _slot_numbers(obj: Any, out: set[float]) -> set[float]:
    if isinstance(obj, bool):
        return out
    if isinstance(obj, int | float):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _slot_numbers(v, out)
    elif isinstance(obj, list | tuple):
        for v in obj:
            _slot_numbers(v, out)
    elif isinstance(obj, str):
        for tok in _NUM_RE.findall(obj):
            out.add(float(tok.replace(",", "")))
    return out


def _matches(text_value: float, slot_value: float) -> bool:
    if text_value == slot_value:
        return True
    rendered = f"{text_value}"
    decimals = len(rendered.split(".")[1]) if "." in rendered else 0
    if round(slot_value, decimals) == text_value:
        return True  # the text shows a rounding of the slot value
    return decimals == 0 and float(int(slot_value)) == text_value


def check_numbers_from_slots(text: str, slots: dict[str, Any]) -> None:
    """Raise ValueError when `text` contains a number that is not (a rounding
    of) a value in `slots`. The structural guarantee behind Phase 4."""
    allowed = _slot_numbers(slots, set())
    for token in _NUM_RE.findall(text):
        value = float(token.replace(",", ""))
        if not any(_matches(value, a) for a in allowed):
            raise ValueError(
                f"generated text contains the number {token!r}, which is absent "
                "from the finding's statement slots — refusing to narrate it"
            )


# ---------------------------------------------------------------------------
# So-what template library — fixed per kind, slot interpolation only
# ---------------------------------------------------------------------------

SO_WHAT: dict[str, str] = {
    "difference": (
        "The gap between {group_high} and {group_low} is the lever — understand "
        "why it exists before trying to move {metric}."
    ),
    "correlation": (
        "Treat {x} as a signal for {y}, not a lever — moving together does not "
        "mean one causes the other."
    ),
    "concentration": (
        "Dependence on {top_group} is a risk: a wobble there moves the whole {metric} number."
    ),
    "driver": (
        "Start any deep-dive on {target} with the top-ranked column — it carries "
        "the most information."
    ),
    "segment": (
        "Target the work where the deviation is: the {segment_column} split "
        "explains most of the spread in {metric}."
    ),
    "trend": (
        "Confirm what happened operationally around the {metric} window before "
        "extrapolating the direction."
    ),
    "anomaly": (
        "Check the flagged day(s) for data errors or real events before trusting "
        "aggregates of {metric}."
    ),
}

# When a finding lacks the slots a template names, fall back to a slot-free line.
SO_WHAT_FALLBACK: dict[str, str] = {
    "difference": "Understand why the groups differ before trying to move the metric.",
    "correlation": "Co-movement is a signal, not a lever — don't assume causation.",
    "concentration": "Heavy dependence on few groups is a risk worth watching.",
    "driver": "Start any deep-dive with the top-ranked column.",
    "segment": "Target the work where the deviation is largest.",
    "trend": "Confirm the operational cause before extrapolating the direction.",
    "anomaly": "Check the flagged days for data errors or real events first.",
}

# Which slot carries the single most quotable number, per kind (first hit wins).
_HEADLINE_KEYS: dict[str, tuple[str, ...]] = {
    "difference": ("relative_difference_pct", "mean_high", "cramers_v"),
    "concentration": ("top_share_pct", "hhi"),
    "trend": ("change_over_window_pct", "slope_per_day", "mean_after"),
    "correlation": ("pearson_r",),
    "driver": (),
    "segment": ("variance_explained_pct",),
    "anomaly": (),
}


@dataclass
class Insight:
    id: str
    finding_id: str
    kind: str
    what_happened: str
    so_what: str
    evidence: dict[str, Any] | None
    confidence: str
    caveats: list[str]
    score: float
    statement_slots: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _effect_norm(kind: str, effect: float) -> float:
    e = abs(effect)
    scale = {
        "difference": 2.0,  # Cohen's d of 2 is enormous
        "correlation": 1.0,
        "concentration": 0.5,  # HHI ≥ 0.5 is extreme
        "driver": 1.0,
        "segment": 1.0,
        "anomaly": 10.0,  # robust z
    }.get(kind)
    if scale is not None:
        return min(1.0, e / scale)
    return min(1.0, math.log10(1.0 + e) / 2.0) if e > 1 else min(1.0, e)  # unit-free squish


def _novelty_key(finding: dict[str, Any]) -> str:
    slots = finding.get("statement_slots") or {}
    subject = slots.get("metric") or slots.get("x") or slots.get("target") or ""
    return f"{finding.get('kind')}::{subject}"


def score_findings(findings: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    """The spec's product score for every finding, novelty decayed per repeated
    (kind, subject). Deterministic; order-stable on ties."""
    max_n = max((int(f.get("n") or 0) for f in findings), default=0)
    seen: dict[str, int] = {}
    scored: list[tuple[float, dict[str, Any]]] = []
    for f in findings:
        key = _novelty_key(f)
        repeats = seen.get(key, 0)
        seen[key] = repeats + 1
        coverage = max(0.1, (int(f.get("n") or 0) / max_n) if max_n else 0.5)
        score = (
            _effect_norm(str(f.get("kind", "")), float(f.get("effect_size") or 0.0))
            * coverage
            * float(f.get("objective_relevance") or 0.5)
            * (_NOVELTY_DECAY**repeats)
            * _CONFIDENCE_WEIGHT.get(str(f.get("confidence")), 0.4)
        )
        scored.append((round(score, 6), f))
    return scored


def rank_insights(findings: list[dict[str, Any]], top_k: int = 5) -> list[Insight]:
    """Top-k findings as Insight cards. Every card's prose is validated
    against its slots at construction — a rogue number raises, never ships."""
    ranked = sorted(enumerate(score_findings(findings)), key=lambda pair: (-pair[1][0], pair[0]))
    cards: list[Insight] = []
    for index, (score, finding) in ranked[:top_k]:
        # The guard basis: statement slots plus the finding's own typed numbers
        # (effect size, p, n) and the two fixed template constants (95% CI,
        # Pareto 80%) — everything else in prose is a violation.
        slots = {
            **(finding.get("statement_slots") or {}),
            "effect_size": finding.get("effect_size"),
            "p_value": finding.get("p_value"),
            "n": finding.get("n"),
            "_ci_level": 95,
            "_pareto_level": 80,
        }
        kind = str(finding.get("kind", ""))
        template = SO_WHAT.get(kind, "Worth a closer look.")
        values = {k: slots.get(k) for k in _template_keys(template)}
        if any(v in (None, "") for v in values.values()):
            # A finding without those slots (e.g. an ANOVA difference has no
            # group pair) must not render "the gap between … and …" (live bug).
            so_what = SO_WHAT_FALLBACK.get(kind, "Worth a closer look before acting on it.")
        else:
            so_what = template.format_map(values)
        what = str(finding.get("phrasing", ""))
        check_numbers_from_slots(so_what, slots)
        check_numbers_from_slots(what, slots)
        cards.append(
            Insight(
                id=f"i-{index + 1}",
                finding_id=str(finding.get("id", "")),
                kind=str(finding.get("kind", "")),
                what_happened=what,
                so_what=so_what,
                evidence=finding.get("evidence"),
                confidence=str(finding.get("confidence", "low")),
                caveats=list(finding.get("caveats") or []),
                score=score,
                statement_slots=slots,
            )
        )
    return cards


def _template_keys(template: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", template))


def executive_summary(
    insights: list[Insight], objective: dict[str, Any] | None = None
) -> dict[str, Any]:
    """3–5 bullets + the single headline number, all guarded against the
    slots of the insights they came from."""
    bullets: list[str] = []
    for card in insights[:_MAX_BULLETS]:
        text = card.what_happened
        if len(text) > _BULLET_CHARS:
            cut = text[:_BULLET_CHARS].rsplit(" ", 1)[0]
            text = f"{cut}…"
        check_numbers_from_slots(text, card.statement_slots)
        bullets.append(text)
    headline: dict[str, Any] | None = None
    for card in insights:
        for key in _HEADLINE_KEYS.get(card.kind, ()):
            value = card.statement_slots.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                headline = {
                    "label": f"{key.replace('_', ' ')} ({card.kind})",
                    "value": value,
                }
                break
        if headline:
            break
    summary: dict[str, Any] = {"bullets": bullets, "headline": headline}
    if objective and objective.get("question"):
        summary["objective"] = str(objective["question"])
    return summary
