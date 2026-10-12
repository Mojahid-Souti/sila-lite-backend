"""Slides Engine v2 — a deterministic legibility gate (WCAG contrast).

Every color pairing the renderer will ever draw comes from a fixed, small
set of palette tokens (three built-in palettes today; brand-derived
tokens in P3). Because the set is closed, legibility can be verified with
the standard WCAG relative-luminance formula instead of a vision-language
model inspecting rendered pixels — deterministic, instant, exhaustive
(every pairing, not just the ones a screenshot happened to sample), and
free of the VRAM/latency cost of loading a second model alongside the
brain model on a 12 GB GPU.

Complexity: O(1) per pairing; O(pairs) for a full palette audit.
"""

from __future__ import annotations

from dataclasses import dataclass

AA_NORMAL_TEXT = 4.5  # WCAG 2.1 AA — body-size text (< 18pt, or < 14pt bold)
AA_LARGE_TEXT = 3.0  # WCAG 2.1 AA — large text (>= 18pt, or >= 14pt bold)


def _srgb_channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of an RRGGBB hex color (no leading '#')."""
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g) + 0.0722 * _srgb_channel(b)


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """WCAG contrast ratio between two RRGGBB hex colors. Always >= 1.0."""
    l1, l2 = relative_luminance(fg_hex) + 0.05, relative_luminance(bg_hex) + 0.05
    lighter, darker = max(l1, l2), min(l1, l2)
    return lighter / darker


def is_large(font_pt: float, *, bold: bool) -> bool:
    """WCAG's "large text" cutoff: >=18pt regular or >=14pt bold."""
    return font_pt >= (14 if bold else 18)


@dataclass(frozen=True)
class TextPairing:
    """One (foreground role, background role, font size, weight) the
    renderer actually draws — named so a failing check points at the
    exact slide element, not just two raw hex codes."""

    label: str
    fg: str
    bg: str
    font_pt: float
    bold: bool = False


# Every text-on-fill pairing render.py produces, across all five layouts.
# Kept here (not derived from render.py) so this module has zero import
# dependency on the renderer — the gate must be checkable before a
# renderer even exists (e.g. for P3's brand-derived tokens).
PAIRINGS: tuple[TextPairing, ...] = (
    TextPairing("title / hero title on background", "text", "bg", 28, bold=True),
    TextPairing("subtitle on background", "muted", "bg", 15),
    TextPairing("card heading on card surface", "text", "surface", 15, bold=True),
    TextPairing("card body on card surface", "muted", "surface", 13),
    TextPairing("metric value on card surface", "accent", "surface", 44, bold=True),
    TextPairing("metric label on card surface", "muted", "surface", 12, bold=True),
    TextPairing("quote text on background", "text", "bg", 26),
    TextPairing("quote label / attribution on background", "muted", "bg", 12),
    TextPairing(
        "accent value on background (title-hero footer items)", "accent", "bg", 32, bold=True
    ),
    TextPairing("table header cell on background", "text", "bg", 11, bold=True),
    TextPairing("table body cell on background", "text", "bg", 11),
    TextPairing("table body cell on banded row (surface)", "text", "surface", 11),
    TextPairing("chart axis labels on background", "muted", "bg", 10),
)


def audit_palette(tokens: object) -> list[str]:
    """Check every known text pairing against a PaletteTokens-shaped
    object. Returns a violation string per failing pairing (empty = the
    palette is legible everywhere the renderer uses it)."""
    violations: list[str] = []
    colors = {
        "text": tokens.text,  # type: ignore[attr-defined]
        "muted": tokens.muted,  # type: ignore[attr-defined]
        "accent": tokens.accent,  # type: ignore[attr-defined]
        "bg": tokens.bg,  # type: ignore[attr-defined]
        "surface": tokens.surface,  # type: ignore[attr-defined]
    }
    for pairing in PAIRINGS:
        required = AA_LARGE_TEXT if is_large(pairing.font_pt, bold=pairing.bold) else AA_NORMAL_TEXT
        ratio = contrast_ratio(colors[pairing.fg], colors[pairing.bg])
        if ratio < required:
            violations.append(
                f"{pairing.label}: contrast {ratio:.2f}:1 is below the WCAG AA "
                f"minimum of {required}:1 for {pairing.font_pt:g}pt"
                f"{' bold' if pairing.bold else ''} text"
            )
    return violations
