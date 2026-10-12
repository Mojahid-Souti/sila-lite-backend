"""The OKR pack's sheet detectors — or inert stand-ins when the pack is absent.

The OKR Reporting Pack (`services/okr/`) is modelled on one customer's files, so
an edition built for wider distribution ships without it. The chat still asks
"does this message carry an OKR sheet?" in two places (the residency gateway
and okr_session); without the pack nothing is an OKR sheet, so the stand-ins
answer False and a conversation can never be marked confidential by it.

Complexity: O(1) — a module import decides which functions are bound.
"""

from __future__ import annotations

try:
    from sila.services.okr.detect import contains_okr_header, is_okr_csv, is_okr_workbook

    OKR_AVAILABLE = True
except ImportError:  # the pack is not part of this edition
    OKR_AVAILABLE = False

    def is_okr_csv(text: str) -> bool:  # noqa: ARG001
        return False

    def is_okr_workbook(data: bytes) -> bool:  # noqa: ARG001
        return False

    def contains_okr_header(text: str) -> bool:  # noqa: ARG001
        return False


__all__ = ["OKR_AVAILABLE", "contains_okr_header", "is_okr_csv", "is_okr_workbook"]
