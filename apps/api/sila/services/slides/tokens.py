"""Slides Engine v2 — design tokens for the three built-in palettes.

Every color, font, and stroke the renderer uses comes from ONE of these
frozen token sets — layouts never invent colors. Brand-token extraction
from a workspace .pptx (Phase 3) will produce the same shape, so a custom
palette drops in without touching the renderer.

Every palette is checked against WCAG AA contrast at import time
(`a11y.audit_palette`) — a palette that fails legibility fails the
process at startup, not a screenshot review after the deck is built.
"""

from __future__ import annotations

from dataclasses import dataclass

from sila.services.slides import a11y
from sila.services.slides.schema import Palette


@dataclass(frozen=True)
class PaletteTokens:
    """Hex colors are RRGGBB without '#' (OOXML srgbClr format)."""

    bg: str  # slide background
    surface: str  # card fill
    stroke: str  # card border
    text: str  # primary text
    muted: str  # secondary text (subtitles, labels)
    accent: str  # brand accent (bars, metrics, headings)
    display_font: str  # titles and hero text
    body_font: str  # everything else


PALETTES: dict[Palette, PaletteTokens] = {
    Palette.MODERN_SLATE: PaletteTokens(
        bg="0F1B2D",
        surface="182A44",
        stroke="24395C",
        text="F5F7FA",
        muted="8FA3BF",
        accent="3B82F6",
        display_font="Calibri Light",
        body_font="Calibri",
    ),
    Palette.MINIMAL_EDITORIAL: PaletteTokens(
        bg="F7F4EC",
        surface="FFFFFF",
        stroke="E4DFD2",
        text="2B2A26",
        muted="6E6A5F",  # darkened from 8A8577 — that shade failed WCAG AA (a11y.py)
        accent="6B7A3F",
        display_font="Georgia",
        body_font="Georgia",
    ),
    Palette.HIGH_CONTRAST_BOLD: PaletteTokens(
        bg="0A0A0A",
        surface="181818",
        stroke="2E2E2E",
        text="FFFFFF",
        muted="9C9C9C",
        accent="C6FF3D",
        display_font="Arial Black",
        body_font="Arial",
    ),
    Palette.MIDNIGHT_TEAL: PaletteTokens(
        bg="0A2A2E",
        surface="123A40",
        stroke="1E4A50",
        text="F0F7F6",
        muted="93BAB6",
        accent="2DD4BF",
        display_font="Calibri Light",
        body_font="Calibri",
    ),
    Palette.WARM_SAND: PaletteTokens(
        bg="F5EFE6",
        surface="FFFFFF",
        stroke="E6DCCB",
        text="2E2A24",
        muted="6B6459",
        accent="B25B33",
        display_font="Georgia",
        body_font="Georgia",
    ),
    Palette.ROYAL_INDIGO: PaletteTokens(
        bg="161734",
        surface="232450",
        stroke="343766",
        text="F2F2FA",
        muted="9EA0C8",
        accent="FBBF24",
        display_font="Calibri Light",
        body_font="Calibri",
    ),
}

for _palette, _tokens in PALETTES.items():
    _violations = a11y.audit_palette(_tokens)
    assert not _violations, f"{_palette.value} palette fails WCAG AA: {_violations}"
