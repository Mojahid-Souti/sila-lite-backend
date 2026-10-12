"""Slides Engine v2 — P3: brand tokens extracted from a corporate .pptx.

Reads the THEME (not per-slide overrides) out of an uploaded .pptx: the
color scheme (dk1/lt1/dk2/lt2/accent1-6) and the font scheme (major/minor
Latin typefaces). Derives our five-role palette from it — background,
card surface, border, text, muted text, accent — using the same
`PaletteTokens` shape the three built-in palettes use, so it drops into
`render.render_deck(deck, tokens=...)` without touching the renderer.

The model never sees or invents these colors; `schema.Palette` stays a
closed enum of the three built-ins. A brand-derived palette is chosen by
the orchestration layer (a workspace with an uploaded brand deck), not
by the model, and is passed as an explicit override.

Legibility is not a hope, it's enforced: every derived role that carries
text is nudged (hue and saturation preserved, only brightness adjusted)
until it clears the same WCAG thresholds `a11y.py` checks the built-in
palettes against, and the finished palette is re-verified with
`a11y.audit_palette` before being returned. This function cannot hand
back a palette that fails the gate — it fixes it or raises.

Known v1 limits (documented, not silently wrong):
  - reads the first theme part only (ppt/theme/theme1.xml, or the first
    themeN.xml found) — a deck with multiple slide masters/themes uses
    just the first;
  - assumes a light-background brand (bg = lt1, text = dk1), the
    overwhelming majority of corporate templates. A deck whose real
    design is dark-on-dark isn't detected — a future pass can compare
    lt1/dk1 luminance to pick a direction if this turns out to matter.

Complexity: O(1) — a handful of XML lookups and bounded color searches.
"""

from __future__ import annotations

import colorsys
import io
import re
import zipfile
from xml.etree import ElementTree

from sila.services.slides import a11y
from sila.services.slides.tokens import PaletteTokens

_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_REQUIRED_SLOTS = ("dk1", "lt1", "accent1")
_ALL_SLOTS = (
    "dk1",
    "lt1",
    "dk2",
    "lt2",
    "accent1",
    "accent2",
    "accent3",
    "accent4",
    "accent5",
    "accent6",
    "hlink",
    "folHlink",
)


class BrandExtractionError(ValueError):
    """The uploaded file isn't usable as a brand source, or the derived
    palette still can't be made legible. Names the problem so the caller
    can fall back to a built-in palette instead of failing silently."""


def _read_theme_xml(pptx_bytes: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(pptx_bytes)) as archive:
            theme_names = sorted(
                n for n in archive.namelist() if re.fullmatch(r"ppt/theme/theme\d+\.xml", n)
            )
            if not theme_names:
                raise BrandExtractionError(
                    "the uploaded file has no ppt/theme part — is it really a .pptx?"
                )
            return archive.read(theme_names[0]).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as exc:
        raise BrandExtractionError(
            "the uploaded file isn't a valid .pptx (not a zip archive)"
        ) from exc


def _color_value(elem: ElementTree.Element | None) -> str | None:
    if elem is None:
        return None
    srgb = elem.find(f"{{{_A_NS}}}srgbClr")
    if srgb is not None:
        return srgb.get("val")
    sys_clr = elem.find(f"{{{_A_NS}}}sysClr")  # e.g. windowText/window with a resolved fallback
    if sys_clr is not None:
        return sys_clr.get("lastClr")
    return None


def _parse_color_scheme(theme_xml: str) -> dict[str, str | None]:
    try:
        root = ElementTree.fromstring(theme_xml)
    except ElementTree.ParseError as exc:
        raise BrandExtractionError(f"theme XML is malformed: {exc}") from exc
    scheme = root.find(f".//{{{_A_NS}}}clrScheme")
    if scheme is None:
        raise BrandExtractionError("theme has no color scheme (clrScheme) to extract")
    colors = {slot: _color_value(scheme.find(f"{{{_A_NS}}}{slot}")) for slot in _ALL_SLOTS}
    missing = [slot for slot in _REQUIRED_SLOTS if not colors[slot]]
    if missing:
        raise BrandExtractionError(f"theme color scheme is missing: {', '.join(missing)}")
    return colors


def _parse_font_scheme(theme_xml: str) -> tuple[str, str]:
    root = ElementTree.fromstring(theme_xml)
    scheme = root.find(f".//{{{_A_NS}}}fontScheme")
    major = minor = None
    if scheme is not None:
        major_elem = scheme.find(f"{{{_A_NS}}}majorFont/{{{_A_NS}}}latin")
        minor_elem = scheme.find(f"{{{_A_NS}}}minorFont/{{{_A_NS}}}latin")
        major = major_elem.get("typeface") if major_elem is not None else None
        minor = minor_elem.get("typeface") if minor_elem is not None else None
    return (major or "Calibri Light"), (minor or "Calibri")


def _blend(hex_a: str, hex_b: str, t: float) -> str:
    """Component-wise lerp in sRGB space from a (t=0) to b (t=1)."""
    a = (int(hex_a[i : i + 2], 16) for i in (0, 2, 4))
    b = (int(hex_b[i : i + 2], 16) for i in (0, 2, 4))
    mixed = (round(av + (bv - av) * t) for av, bv in zip(a, b, strict=True))
    return "".join(f"{v:02X}" for v in mixed)


def _hex_to_hsv(hex_color: str) -> tuple[float, float, float]:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(r, g, b)


def _hsv_to_hex(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h, s, max(0.0, min(1.0, v)))
    return f"{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}"


def _ensure_min_contrast(fg_hex: str, backgrounds: list[str], min_ratio: float) -> str:
    """Nudge fg_hex's brightness (hue and saturation held fixed) until its
    contrast ratio clears min_ratio against EVERY background. Direction
    (darker vs lighter) is fixed once, toward whichever end the
    backgrounds' average luminance calls for, then scanned in small
    steps — contrast is monotonic in that direction once fixed."""
    if all(a11y.contrast_ratio(fg_hex, bg) >= min_ratio for bg in backgrounds):
        return fg_hex

    h, s, v = _hex_to_hsv(fg_hex)
    avg_luminance = sum(a11y.relative_luminance(bg) for bg in backgrounds) / len(backgrounds)
    direction = -1 if avg_luminance > 0.5 else 1  # darken on a light bg, lighten on a dark bg

    for step in range(1, 101):
        candidate = _hsv_to_hex(h, s, v + direction * step / 100)
        if all(a11y.contrast_ratio(candidate, bg) >= min_ratio for bg in backgrounds):
            return candidate

    for candidate in (_hsv_to_hex(h, s, 0.0), _hsv_to_hex(h, s, 1.0), "000000", "FFFFFF"):
        if all(a11y.contrast_ratio(candidate, bg) >= min_ratio for bg in backgrounds):
            return candidate
    raise BrandExtractionError(
        f"no shade of {fg_hex} is legible against {backgrounds} — the source theme "
        "doesn't have enough contrast to derive a usable palette from"
    )


def _derive_surface(bg: str, dk1: str, lt2: str | None) -> str:
    """Prefer the theme's own secondary-light color if it actually reads
    as a distinct fill; otherwise synthesize a subtle card tint."""
    if lt2 and abs(a11y.relative_luminance(lt2) - a11y.relative_luminance(bg)) > 0.02:
        return lt2
    return _blend(bg, dk1, 0.06)


def extract_brand_tokens(pptx_bytes: bytes) -> PaletteTokens:
    """An uploaded corporate .pptx -> a legible PaletteTokens. Raises
    BrandExtractionError if the file isn't a usable .pptx, or (after
    every legibility correction) the source theme still can't produce a
    palette that passes WCAG AA — the caller should fall back to a
    built-in palette rather than ship an illegible one."""
    theme_xml = _read_theme_xml(pptx_bytes)
    colors = _parse_color_scheme(theme_xml)
    major_font, minor_font = _parse_font_scheme(theme_xml)

    bg = colors["lt1"]
    stroke = _blend(bg, colors["dk1"], 0.14)
    surface = _derive_surface(bg, colors["dk1"], colors.get("lt2"))
    text = _ensure_min_contrast(colors["dk1"], [bg, surface], a11y.AA_LARGE_TEXT)
    muted = _ensure_min_contrast(_blend(text, bg, 0.55), [bg, surface], a11y.AA_NORMAL_TEXT)
    accent = _ensure_min_contrast(colors["accent1"], [bg, surface], a11y.AA_LARGE_TEXT)

    tokens = PaletteTokens(
        bg=bg,
        surface=surface,
        stroke=stroke,
        text=text,
        muted=muted,
        accent=accent,
        display_font=major_font,
        body_font=minor_font,
    )
    violations = a11y.audit_palette(tokens)
    if violations:  # defensive — the derivation above should already prevent this
        raise BrandExtractionError(f"derived brand palette still fails WCAG AA: {violations}")
    return tokens
