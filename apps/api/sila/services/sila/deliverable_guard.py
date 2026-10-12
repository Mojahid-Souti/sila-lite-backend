"""The security gateway on what a turn HANDS BACK.

The Phase-6 gatekeeper vets an agent when it enters review. This is the same
scanner pointed at the deliverables of a single turn — a deck, a document, a
draft agent's interview — at the moment they are produced, before the user is
offered them. Two reasons it exists as its own gate:

1. The orchestration model now runs in the cloud. Content the user typed or
   attached has already left the building by the time an artifact exists, but
   the artifact is what gets FORWARDED — to a panel, a customer, a mailbox. A
   national ID inside slide 4 is a PDPL incident waiting for a click.
2. Vetting happens once, on the agent. Artifacts are produced every turn and
   never enter that pipeline at all.

Findings ride on the artifact as data (`security`), so the UI can refuse the
click, show why, and the model can say exactly WHERE to edit. Only the redacted
preview ever leaves this module — the raw match is the user's data.

Complexity: O(n × r) over the text for r regex recognizers, per artifact.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog

from sila.adapters.scanner.base import Finding, ScanInput
from sila.adapters.scanner.pii import PIIScanner

log = structlog.get_logger(__name__)

# The scanner is local regex, so this is a backstop against a pathological
# input or a future adapter that talks to a service — not a normal wait.
_SCAN_TIMEOUT_S = 20.0

# Severities that make an artifact UNCLICKABLE. Below this the finding is shown
# (an amber notice) but the file still opens — an 8-digit number with no label
# is more often an invoice than an identity.
_BLOCKING = frozenset({"high", "critical"})

# What the UI and the model call each finding type. Falls back to a humanised
# version of the id for a recognizer added later.
_LABELS: dict[str, str] = {
    "pii.oman_national_id": "Omani national ID",
    "pii.oman_mobile": "Omani mobile number",
    "pii.oman_iban": "Omani IBAN",
    "pii.oman_vehicle_plate": "vehicle plate",
    "pii.email": "email address",
    "pii.phone": "phone number",
    "pii.credit_card": "card number",
    "pii.iban": "IBAN",
}

_MAX_FINDINGS = 12  # enough to act on; a dump of 200 helps nobody


@dataclass(frozen=True)
class Chunk:
    """One labelled piece of a deliverable — a slide, a section, a slot."""

    location: str  # "Slide 3", "Section 'Background'", "Purpose"
    text: str


def _label(finding_type: str) -> str:
    if finding_type in _LABELS:
        return _LABELS[finding_type]
    tail = finding_type.rsplit(".", 1)[-1]
    return tail.replace("_", " ")


def _public(finding: Finding, location: str) -> dict[str, Any]:
    """The finding as the UI sees it. `matched_full` deliberately absent."""
    return {
        "type": finding.finding_type,
        "label": _label(finding.finding_type),
        "severity": finding.severity,
        "preview": finding.matched_preview,
        "location": location,
    }


async def guard_chunks(chunks: list[Chunk]) -> dict[str, Any]:
    """Scan every chunk; return `{status, findings}`.

    status is "clear" (nothing), "notice" (only low/medium — shown, still
    openable) or "blocked" (a high/critical hit — the card cannot be opened).
    Findings carry the chunk's human location so the model can point at it.
    """
    scanner = PIIScanner()
    public: list[dict[str, Any]] = []
    blocking = False
    for chunk in chunks:
        if not chunk.text.strip():
            continue
        try:
            async with asyncio.timeout(_SCAN_TIMEOUT_S):
                found = await scanner.scan(ScanInput(system_prompt=chunk.text))
        except (Exception, asyncio.CancelledError) as exc:
            # A scan that cannot complete is NOT a pass. Before this, the
            # exception escaped the tool call and took the finished artifact
            # with it: the user got a generic failure and no card at all, for
            # a file that was already built and stored. Now the deliverable
            # still appears — blocked, and saying why.
            log.error(
                "deliverable_guard.scan_failed",
                location=chunk.location,
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            return {
                "status": "blocked",
                "findings": [
                    {
                        "type": "scan.failed",
                        "label": "security scan did not complete",
                        "severity": "critical",
                        "preview": type(exc).__name__,
                        "location": chunk.location,
                    }
                ],
            }
        for f in found:
            if f.severity in _BLOCKING:
                blocking = True
            public.append(_public(f, chunk.location))
    # Worst first, so the tooltip leads with what actually blocked the card.
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    public.sort(key=lambda f: order.get(str(f["severity"]), 5))
    status = "blocked" if blocking else ("notice" if public else "clear")
    return {"status": status, "findings": public[:_MAX_FINDINGS]}


async def guard_text(text: str, *, location: str) -> dict[str, Any]:
    """One unlabelled body — a whole document as a single chunk."""
    return await guard_chunks([Chunk(location=location, text=text)])


def describe_for_model(security: dict[str, Any], *, artifact: str) -> str:
    """The sentence the model is given so it can tell the user WHERE to edit.

    Empty when clear — the model should not mention a gate that found nothing.
    Groups by location so "Slide 3" is said once, not per finding.
    """
    findings = security.get("findings") or []
    if not findings:
        return ""
    by_loc: dict[str, list[str]] = {}
    for f in findings:
        by_loc.setdefault(str(f["location"]), []).append(f"{f['label']} ({f['preview']})")
    where = "; ".join(f"{loc}: {', '.join(items)}" for loc, items in by_loc.items())
    if any(str(f.get("type")) == "scan.failed" for f in findings):
        return (
            f"SECURITY GATEWAY COULD NOT SCAN '{artifact}' — the check itself failed, so "
            "the card is locked. This is NOT a finding of personal data: say the scan "
            "could not complete, that the file is held back as a precaution rather than "
            "because anything was found, and offer to try again."
        )
    if security.get("status") == "blocked":
        return (
            f"SECURITY GATEWAY BLOCKED '{artifact}' — it contains personal data and the "
            f"card is NOT openable until fixed. Found: {where}. Tell the user plainly "
            "that the gateway blocked it, list each location and what was found, and "
            "offer to regenerate with those values removed or replaced with placeholders. "
            "Do NOT reproduce the raw values yourself."
        )
    return (
        f"Security gateway notice on '{artifact}' (openable): {where}. Mention it in "
        "one line and offer to remove those values if the user wants."
    )


def deck_chunks(deck: Any) -> list[Chunk]:
    """A validated Deck → one chunk per slide, numbered as the user sees them."""
    out: list[Chunk] = []
    for i, slide in enumerate(getattr(deck, "slides", []) or [], start=1):
        parts: list[str] = [
            str(getattr(slide, "slide_title", "") or ""),
            str(getattr(slide, "subtitle", "") or ""),
        ]
        for c in getattr(slide, "containers", []) or []:
            parts.append(str(getattr(c, "heading", "") or ""))
            parts.append(str(getattr(c, "body", "") or ""))
            parts.append(str(getattr(c, "accent_value", "") or ""))
            for row in getattr(c, "rows", []) or []:
                parts.append(" ".join(str(cell) for cell in row))
        out.append(Chunk(location=f"Slide {i}", text="\n".join(p for p in parts if p)))
    return out


def section_chunks(title: str, sections: list[tuple[str, str]]) -> list[Chunk]:
    """A document's (heading, body) sections → labelled chunks."""
    out = [Chunk(location="Title", text=title)]
    for i, (heading, body) in enumerate(sections, start=1):
        loc = f"Section '{heading}'" if heading.strip() else f"Section {i}"
        out.append(Chunk(location=loc, text=f"{heading}\n{body}"))
    return out


def agent_chunks(purpose: str, filled: dict[str, Any]) -> list[Chunk]:
    """A draft agent's purpose plus every interview answer, each by slot name."""
    out = [Chunk(location="Purpose", text=purpose)]
    for slot, value in (filled or {}).items():
        if isinstance(value, str) and value.strip():
            out.append(Chunk(location=f"Answer '{slot}'", text=value))
    return out
