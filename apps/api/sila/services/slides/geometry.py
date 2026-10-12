"""Slides Engine v2 — shared geometry: margins, grid math, and the
column-count rule both the renderer (render.py) and the character-
capacity backstop (reflow.py) need to agree on. One source of truth so
a layout's on-slide geometry and its capacity estimate can never drift
apart.

Complexity: O(n) for a grid layout; O(1) everything else.
"""

from __future__ import annotations

from sila.services.slides.enums import LayoutType
from sila.services.slides.pptx import SLIDE_W

MARGIN_X = 800000  # left/right margin, EMU
MARGIN_Y = 550000  # top/bottom margin, EMU
GAP = 250000  # gutter between grid cells, EMU
PAD = 300000  # inner card padding, EMU
CONTENT_W = SLIDE_W - 2 * MARGIN_X

MAX_CONTAINERS = 4  # the 8-second rule: <= 4 information chunks per slide

# Font sizes in hundredths of a point (OOXML sz units) — one named
# constant per (layout, role), so reflow.py's capacity math always
# matches what render.py actually draws.
TITLE_SZ = 2800
HERO_TITLE_SZ = 5200
HERO_SUBTITLE_SZ = 2000
SPLIT_TITLE_SZ = 3200
SUBTITLE_SZ = 1500
CARD_HEADING_SZ = 1500
CARD_BODY_SZ = 1300
CARD_ACCENT_SZ = 3200
METRIC_VALUE_SZ = 4400
METRIC_LABEL_SZ = 1200
METRIC_BODY_SZ = 1150
QUOTE_TEXT_SZ = 2600
QUOTE_LABEL_SZ = 1200
QUOTE_ATTRIBUTION_SZ = 1500


def grid(n: int, x: int, y: int, w: int, h: int, cols: int) -> list[tuple[int, int, int, int]]:
    """n cells left-to-right, top-to-bottom in a cols-wide grid inside (x, y, w, h)."""
    rows = (n + cols - 1) // cols
    cell_w = (w - (cols - 1) * GAP) // cols
    cell_h = (h - (rows - 1) * GAP) // rows
    return [
        (x + (i % cols) * (cell_w + GAP), y + (i // cols) * (cell_h + GAP), cell_w, cell_h)
        for i in range(n)
    ]


def columns_for(layout: LayoutType, n: int) -> int:
    """How many grid columns a layout uses for n containers — the same
    rule render.py applies, reused here so a capacity estimate always
    matches the cell size a container actually gets."""
    if n <= 0:
        return 1
    if layout is LayoutType.THREE_COLUMN_CARDS:
        return n if n <= 3 else 2  # 4 containers -> a balanced 2x2
    if layout is LayoutType.SPLIT_GRID:
        return 1  # a single stacked column
    if layout is LayoutType.DATA_SHOWCASE:
        return min(n, 3)  # caption cards under the figure
    return n  # title-hero footer, metric-showcase: one column per item
