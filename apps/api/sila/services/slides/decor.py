"""Slides Engine v2 — decorative geometry (the "not plain" layer).

Subtle, palette-bound adornments drawn BEHIND the content of every slide
so a plain title/cards/metric page reads as designed, not as "colored bg
+ text": a large very-translucent accent circle bleeding from the
top-right corner, and a short accent rule in the bottom margin. Both live
outside the content area and at low alpha, so nothing sits under text and
the WCAG contrast the a11y gate guarantees is untouched.

data-showcase is left clean — a translucent wash behind a chart or table
would fight its own backdrop.

Complexity: O(1) — a fixed handful of shapes per slide.
"""

from __future__ import annotations

from sila.services.slides.enums import LayoutType
from sila.services.slides.geometry import MARGIN_X
from sila.services.slides.pptx import SLIDE_H, SLIDE_W, ShapeList
from sila.services.slides.tokens import PaletteTokens

_BLOB_ALPHA = 7000  # 7% — a whisper of accent, never enough to affect text
_BLOB = 4200000  # diameter of the corner wash, EMU

# The corner wash reads as clutter on a full-bleed figure; keep those clean.
_NO_BLOB = frozenset({LayoutType.DATA_SHOWCASE})


def apply(shapes: ShapeList, tokens: PaletteTokens, layout: LayoutType) -> None:
    """Draw the backdrop for a slide. Called BEFORE the layout's content so
    everything here sits behind it (spTree order = z-order)."""
    if layout not in _NO_BLOB:
        # A big soft accent circle bleeding off the top-right corner.
        shapes.preset(
            SLIDE_W - _BLOB + 1100000,
            -_BLOB // 2,
            _BLOB,
            _BLOB,
            "ellipse",
            fill=tokens.accent,
            alpha=_BLOB_ALPHA,
        )
    # A short accent rule sitting in the bottom margin, below all content.
    shapes.rect(MARGIN_X, SLIDE_H - 300000, 760000, 45000, tokens.accent)
