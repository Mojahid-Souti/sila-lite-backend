"""Omani-context PII recognizers.

Each recognizer is a `re.Pattern` with a finding_type label + redactor.
Patterns are written from public-domain documentation (Royal Oman Police ID
formats, CBO IBAN registry, ITU country code allocations). No real citizen
data is hardcoded.

Kept as a plain regex library — Presidio integration in `pii.py` wraps these
into `PatternRecognizer` instances at load time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class OmaniRecognizer:
    """One Omani PII pattern + its redaction strategy."""

    finding_type: str
    pattern: re.Pattern[str]
    severity: str  # info | low | medium | high | critical
    score: float  # Presidio confidence score (0..1)

    def redact(self, raw: str) -> str:
        """Return a safe-to-display preview. Default: keep last 2 chars."""
        if len(raw) <= 4:
            return "*" * len(raw)
        return raw[:2] + "*" * (len(raw) - 4) + raw[-2:]


# Omani national ID — Royal Oman Police format: 8 digits.
# Optionally prefixed with the citizenship marker. Use word boundaries to
# avoid matching arbitrary 8-digit numbers in URLs/timestamps.
OMAN_NATIONAL_ID = OmaniRecognizer(
    finding_type="pii.oman_national_id",
    pattern=re.compile(r"\b\d{8}\b"),
    severity="critical",
    score=0.65,  # purely numeric — high false-positive rate; rely on context boost
)

# Omani mobile numbers — country code +968, then 9X (mobile) or 7X (newer ranges).
# Optional spaces or hyphens between groups.
OMAN_MOBILE = OmaniRecognizer(
    finding_type="pii.oman_mobile",
    pattern=re.compile(r"(?:\+?968[\s-]?)?(?:9|7)\d(?:[\s-]?\d){6}\b"),
    severity="high",
    score=0.85,
)

# Omani IBAN — OM + 2 check digits + 4 alpha + 16 numeric (22 chars total).
OMAN_IBAN = OmaniRecognizer(
    finding_type="pii.oman_iban",
    pattern=re.compile(r"\bOM\d{2}[A-Z]{4}\d{16}\b"),
    severity="critical",
    score=0.99,
)

# Vehicle plate — public-domain RoP format: 1-6 digits, optionally followed by
# 1-3 Latin letters (rendered when transcribed; Arabic letters not handled here).
OMAN_PLATE = OmaniRecognizer(
    finding_type="pii.oman_vehicle_plate",
    pattern=re.compile(r"\b\d{1,6}\s?[A-Z]{1,3}\b"),
    severity="medium",
    score=0.4,  # low confidence without context; ranking depends on label proximity
)

# Order matters only for the test corpus convenience — service runs all of them.
ALL_RECOGNIZERS: tuple[OmaniRecognizer, ...] = (
    OMAN_NATIONAL_ID,
    OMAN_MOBILE,
    OMAN_IBAN,
    OMAN_PLATE,
)
