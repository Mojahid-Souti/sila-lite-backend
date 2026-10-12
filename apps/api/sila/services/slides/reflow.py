"""Slides Engine v2 — P2: the geometry-derived character-capacity backstop.

The word-count density limits in schema.py (8-word titles, 5-word
headings, 25-word bodies) are a deliberate editorial rule — the
"8-second rule" for a scannable slide — not a physical-fit calculation.
This module adds the fit calculation: for each layout, how many
characters actually fit in the box that role renders into, derived from
the same geometry (`geometry.py`) the renderer uses. schema.py applies
whichever limit is stricter, so an unusually long-worded title, or a
layout with an unusually narrow box, can never overflow even when it
satisfies the word count.

Container capacity is always computed for the worst case: a slide
paginated at MAX_CONTAINERS per page (the smallest cell a container can
ever end up in, see geometry.columns_for). A body chunk sized to that
budget is safe no matter how many siblings pagination later groups it
with — no fixed-point iteration needed.

Complexity: O(1) per capacity lookup (a handful of arithmetic ops).
"""

from __future__ import annotations

from sila.services.slides import geometry
from sila.services.slides.enums import LayoutType
from sila.services.slides.pptx import SLIDE_H, SLIDE_W

_EMU_PER_PT = 12700


def estimate_char_capacity(
    width_emu: int,
    height_emu: int,
    font_pt: float,
    *,
    line_height_ratio: float = 1.25,
    avg_char_width_ratio: float = 0.52,
) -> int:
    """How many characters fit in a box this size at this font size — a
    standard proportional-font heuristic (average glyph width ~0.52x the
    point size), not a text-shaping engine. Good enough to catch real
    overflow; exact kerning doesn't change the reflow decision."""
    width_pt = width_emu / _EMU_PER_PT
    height_pt = height_emu / _EMU_PER_PT
    chars_per_line = max(1, int(width_pt / (font_pt * avg_char_width_ratio)))
    max_lines = max(1, int(height_pt / (font_pt * line_height_ratio)))
    return chars_per_line * max_lines


# The shared top-of-content y for THREE_COLUMN_CARDS / METRIC_SHOWCASE's
# title block (render._title_block), computed for the worst case
# (subtitle present) — so the capacity estimate is always conservative,
# never optimistic, when a slide happens to have no subtitle.
_TITLE_BLOCK_TOP = geometry.MARGIN_Y + 1150000 + 620000 + 150000

_TITLE_BOX: dict[LayoutType, tuple[int, int, int]] = {
    LayoutType.TITLE_HERO: (geometry.CONTENT_W, 1500000, geometry.HERO_TITLE_SZ),
    LayoutType.SPLIT_GRID: (4600000, 2200000, geometry.SPLIT_TITLE_SZ),
    LayoutType.THREE_COLUMN_CARDS: (geometry.CONTENT_W, 900000, geometry.TITLE_SZ),
    LayoutType.METRIC_SHOWCASE: (geometry.CONTENT_W, 900000, geometry.TITLE_SZ),
    LayoutType.QUOTE_FOCUS: (geometry.CONTENT_W, 500000, geometry.QUOTE_LABEL_SZ),
    LayoutType.DATA_SHOWCASE: (geometry.CONTENT_W, 900000, geometry.TITLE_SZ),
    LayoutType.PROCESS_FLOW: (geometry.CONTENT_W, 900000, geometry.TITLE_SZ),
    LayoutType.COMPARISON: (geometry.CONTENT_W, 900000, geometry.TITLE_SZ),
    LayoutType.TIMELINE: (geometry.CONTENT_W, 900000, geometry.TITLE_SZ),
    LayoutType.IMAGE_FEATURE: (5200000, 1800000, geometry.SPLIT_TITLE_SZ),
}

_SUBTITLE_BOX: dict[LayoutType, tuple[int, int, int]] = {
    LayoutType.TITLE_HERO: (geometry.CONTENT_W, 800000, geometry.HERO_SUBTITLE_SZ),
    LayoutType.SPLIT_GRID: (4600000, 1600000, geometry.SUBTITLE_SZ),
    LayoutType.THREE_COLUMN_CARDS: (geometry.CONTENT_W, 550000, geometry.SUBTITLE_SZ),
    LayoutType.METRIC_SHOWCASE: (geometry.CONTENT_W, 550000, geometry.SUBTITLE_SZ),
    LayoutType.DATA_SHOWCASE: (geometry.CONTENT_W, 550000, geometry.SUBTITLE_SZ),
    LayoutType.PROCESS_FLOW: (geometry.CONTENT_W, 550000, geometry.SUBTITLE_SZ),
    LayoutType.COMPARISON: (geometry.CONTENT_W, 550000, geometry.SUBTITLE_SZ),
    LayoutType.TIMELINE: (geometry.CONTENT_W, 550000, geometry.SUBTITLE_SZ),
    LayoutType.IMAGE_FEATURE: (5200000, 1400000, geometry.SUBTITLE_SZ),
}


def title_capacity(layout: LayoutType) -> int:
    w, h, sz = _TITLE_BOX[layout]
    return estimate_char_capacity(w, h, sz / 100)


def subtitle_capacity(layout: LayoutType) -> int | None:
    """None means this layout has no dedicated subtitle box (quote-focus
    folds `subtitle` into the quote body instead) — nothing to check."""
    box = _SUBTITLE_BOX.get(layout)
    if box is None:
        return None
    w, h, sz = box
    return estimate_char_capacity(w, h, sz / 100)


def _worst_case_cell(layout: LayoutType) -> tuple[int, int]:
    """The smallest cell a container can get: this layout's grid sized
    for MAX_CONTAINERS — the most any single page ever holds."""
    n = geometry.MAX_CONTAINERS
    cols = geometry.columns_for(layout, n)
    if layout is LayoutType.TITLE_HERO:
        cells = geometry.grid(n, 0, 0, geometry.CONTENT_W, 1050000, cols)
    elif layout is LayoutType.SPLIT_GRID:
        left_w = 4600000
        right_x = geometry.MARGIN_X + left_w + 400000
        right_w = SLIDE_W - geometry.MARGIN_X - right_x
        cells = geometry.grid(n, 0, 0, right_w, SLIDE_H - 2 * geometry.MARGIN_Y, cols)
    elif layout is LayoutType.METRIC_SHOWCASE:
        top = _TITLE_BLOCK_TOP + 200000
        cells = geometry.grid(n, 0, 0, geometry.CONTENT_W, SLIDE_H - geometry.MARGIN_Y - top, cols)
    else:  # THREE_COLUMN_CARDS
        cells = geometry.grid(
            n, 0, 0, geometry.CONTENT_W, SLIDE_H - geometry.MARGIN_Y - _TITLE_BLOCK_TOP, cols
        )
    _, _, w, h = cells[0]
    return w, h


def container_capacity(layout: LayoutType) -> tuple[int, int]:
    """(heading_chars, body_chars) — the worst-case per-container budget
    for this layout, independent of how many containers a given slide
    actually has (see module docstring: always sized for the tightest
    packing pagination could ever produce)."""
    if layout is LayoutType.QUOTE_FOCUS:
        # A single big quote box, not a grid cell.
        cell_w, cell_h = geometry.CONTENT_W - 800000, 2500000
        return (
            estimate_char_capacity(cell_w, 500000, geometry.QUOTE_ATTRIBUTION_SZ / 100),
            estimate_char_capacity(cell_w, cell_h, geometry.QUOTE_TEXT_SZ / 100),
        )

    cell_w, cell_h = _worst_case_cell(layout)
    cell_w -= 2 * geometry.PAD
    cell_h -= 2 * geometry.PAD
    heading_sz = geometry.CARD_HEADING_SZ / 100
    body_sz = (
        geometry.METRIC_BODY_SZ if layout is LayoutType.METRIC_SHOWCASE else geometry.CARD_BODY_SZ
    ) / 100
    heading_h = min(cell_h, int(2 * heading_sz * _EMU_PER_PT * 1.25))  # ~2 lines, capped
    heading_capacity = estimate_char_capacity(cell_w, heading_h, heading_sz)
    body_h = max(cell_h - heading_h, int(body_sz * _EMU_PER_PT))
    body_capacity = estimate_char_capacity(cell_w, body_h, body_sz)
    return heading_capacity, body_capacity
