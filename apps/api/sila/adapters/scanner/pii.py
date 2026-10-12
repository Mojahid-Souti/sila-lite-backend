"""PII scanner — wraps the Omani recognizers.

Kept regex-only for this slice: deterministic, zero-cost cold start, no
ML model dependency at app boot. The interface mirrors what a full Presidio
backed adapter would expose, so swapping in `presidio_analyzer.AnalyzerEngine`
later is a no-API-change replacement.

Why deterministic regex first (vs. Presidio NlpEngine):
  - 60s cold-start budget per CLAUDE.md Phase 6 — Presidio + spaCy load is ~5s
  - All Omani-context detection is regex anyway (national ID, IBAN, mobile)
  - Generic name/email detection added when Phase 6 part 2 brings in Presidio
"""

from __future__ import annotations

import re
from typing import Any, Final

from .base import AgentScanner, Finding, ScanInput
from .recognizers.oman import ALL_RECOGNIZERS

# Best-effort context windowing — keep up to 40 chars around the match
# in `metadata.context` so a reviewer can judge severity without exposing
# the raw match (which lives in `matched_full`).
CONTEXT_WIDTH: Final[int] = 40


class PIIScanner(AgentScanner):
    name = "pii"

    def __init__(self, recognizers: tuple[Any, ...] = ALL_RECOGNIZERS) -> None:
        self._recognizers = recognizers

    async def scan(self, agent_input: ScanInput) -> list[Finding]:
        """Run every recognizer over every input location. O(n × r)."""
        findings: list[Finding] = []
        for location, text in (
            ("system_prompt", agent_input.system_prompt),
            ("sample_input", agent_input.sample_input),
        ):
            if not text:
                continue
            findings.extend(self._scan_text(location, text))
        return findings

    def _scan_text(self, location: str, text: str) -> list[Finding]:
        out: list[Finding] = []
        for rec in self._recognizers:
            for m in rec.pattern.finditer(text):
                raw = m.group(0)
                start, end = m.span()
                ctx_start = max(0, start - CONTEXT_WIDTH)
                ctx_end = min(len(text), end + CONTEXT_WIDTH)
                context = text[ctx_start:ctx_end].replace("\n", " ")
                # Recognizers below 0.7 are ambiguous on their own (any 8-digit
                # number or 1-6 digit + letters could be coincidence). Require a
                # nearby label keyword to reduce false positives.
                if rec.score < 0.7 and not _has_pii_label(context):
                    continue
                out.append(
                    Finding(
                        finding_type=rec.finding_type,
                        severity=rec.severity,
                        location=location,
                        matched_full=raw,
                        matched_preview=rec.redact(raw),
                        metadata={"context": context, "score": rec.score},
                    )
                )
        return out


# Words that hint a numeric blob is actually PII vs. a coincidence
# (e.g. "phone", "mobile", "ID", "plate"). Used to reduce false-positives
# on weak recognizers like the vehicle-plate one.
_PII_LABELS = re.compile(
    r"\b(id|national|civil|phone|mobile|tel|plate|iban|بطاقة|هاتف|جوال)\b",
    re.IGNORECASE,
)


def _has_pii_label(context: str) -> bool:
    return _PII_LABELS.search(context) is not None
