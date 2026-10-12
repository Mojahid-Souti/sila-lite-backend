"""Slides Engine v2 — P1: the deterministic renderer.

Six layout components, each a pure function (validated Slide + palette
tokens) → positioned OOXML shapes. All geometry is computed here in EMUs;
the model never controls position, size, or color — it only chose the
layout, the palette, and the (density-gated) text. Same deck in → same
bytes out (pixel-stable; verified by tests).

data-showcase renders one native figure (a table or an embedded chart)
plus an optional caption strip — the figure's data and colors also come
only from the validated spec and the palette tokens.

Complexity: O(slides × containers) — a few string builds per shape.
"""

from __future__ import annotations

from sila.services.slides import chartpart, decor, geometry, icons
from sila.services.slides.enums import ContainerType
from sila.services.slides.geometry import CONTENT_W as _CONTENT_W
from sila.services.slides.geometry import MARGIN_X as _MX
from sila.services.slides.geometry import MARGIN_Y as _MY
from sila.services.slides.geometry import PAD as _PAD
from sila.services.slides.geometry import columns_for
from sila.services.slides.geometry import grid as _grid
from sila.services.slides.pptx import (
    SLIDE_H,
    SLIDE_W,
    Para,
    RenderedSlide,
    Run,
    ShapeList,
    TableStyle,
    package,
    slide_part,
)
from sila.services.slides.schema import Container, Deck, LayoutType, Slide
from sila.services.slides.tokens import PALETTES, PaletteTokens


def _table_style(t: PaletteTokens) -> TableStyle:
    return TableStyle(
        header_text=t.text,
        header_rule=t.accent,
        row_fill=t.bg,
        row_alt_fill=t.surface,
        body_text=t.text,
        stroke=t.stroke,
        font=t.body_font,
    )


def _card_paras(c: Container, t: PaletteTokens, *, align: str = "l") -> list[Para]:
    """The shared card interior: optional metric value, heading, body."""
    paras: list[Para] = []
    if c.accent_value.strip():
        paras.append(
            Para(
                (
                    Run(
                        c.accent_value, geometry.CARD_ACCENT_SZ, t.accent, t.display_font, bold=True
                    ),
                ),
                align=align,
            )
        )
    if c.heading.strip():
        paras.append(
            Para(
                (Run(c.heading, geometry.CARD_HEADING_SZ, t.text, t.body_font, bold=True),),
                align=align,
                space_before=4 if paras else 0,
            )
        )
    if c.body.strip():
        paras.append(
            Para(
                (Run(c.body, geometry.CARD_BODY_SZ, t.muted, t.body_font),),
                align=align,
                space_before=4 if paras else 0,
            )
        )
    return paras


def _title_block(shapes: ShapeList, slide: Slide, t: PaletteTokens) -> int:
    """Standard top-of-slide title + optional subtitle. Returns content top y."""
    shapes.rect(_MX, _MY, 900000, 76200, t.accent)
    shapes.text(
        _MX,
        _MY + 200000,
        _CONTENT_W,
        900000,
        [Para((Run(slide.slide_title, geometry.TITLE_SZ, t.text, t.display_font, bold=True),))],
    )
    y = _MY + 1150000
    if slide.subtitle.strip():
        shapes.text(
            _MX,
            y,
            _CONTENT_W,
            550000,
            [Para((Run(slide.subtitle, geometry.SUBTITLE_SZ, t.muted, t.body_font),))],
        )
        y += 620000
    return y + 150000


def _title_hero(shapes: ShapeList, slide: Slide, t: PaletteTokens) -> None:
    shapes.rect(_MX, 2050000, 1400000, 90000, t.accent)
    shapes.text(
        _MX,
        2320000,
        _CONTENT_W,
        1500000,
        [
            Para(
                (Run(slide.slide_title, geometry.HERO_TITLE_SZ, t.text, t.display_font, bold=True),)
            )
        ],
    )
    if slide.subtitle.strip():
        shapes.text(
            _MX,
            3900000,
            _CONTENT_W,
            800000,
            [Para((Run(slide.subtitle, geometry.HERO_SUBTITLE_SZ, t.muted, t.body_font),))],
        )
    if slide.containers:
        cells = _grid(
            len(slide.containers), _MX, 5300000, _CONTENT_W, 1050000, len(slide.containers)
        )
        for c, (x, y, w, h) in zip(slide.containers, cells, strict=True):
            shapes.text(x, y, w, h, _card_paras(c, t), anchor="b")


def _split_grid(shapes: ShapeList, slide: Slide, t: PaletteTokens) -> None:
    left_w = 4600000
    shapes.rect(_MX, 900000, 900000, 76200, t.accent)
    shapes.text(
        _MX,
        1150000,
        left_w,
        2200000,
        [
            Para(
                (
                    Run(
                        slide.slide_title,
                        geometry.SPLIT_TITLE_SZ,
                        t.text,
                        t.display_font,
                        bold=True,
                    ),
                )
            )
        ],
    )
    if slide.subtitle.strip():
        shapes.text(
            _MX,
            3400000,
            left_w,
            1600000,
            [Para((Run(slide.subtitle, geometry.SUBTITLE_SZ, t.muted, t.body_font),))],
        )
    right_x = _MX + left_w + 400000
    right_w = SLIDE_W - _MX - right_x
    n = len(slide.containers)
    if n:
        for c, (x, y, w, h) in zip(
            slide.containers, _grid(n, right_x, _MY, right_w, SLIDE_H - 2 * _MY, 1), strict=True
        ):
            shapes.rect(x, y, w, h, t.surface, stroke=t.stroke, rounded=True)
            shapes.text(
                x + _PAD, y + _PAD, w - 2 * _PAD, h - 2 * _PAD, _card_paras(c, t), anchor="ctr"
            )


def _column_cards(shapes: ShapeList, slide: Slide, t: PaletteTokens) -> None:
    top = _title_block(shapes, slide, t)
    n = len(slide.containers)
    if n:
        cells = _grid(
            n,
            _MX,
            top,
            _CONTENT_W,
            SLIDE_H - _MY - top,
            columns_for(LayoutType.THREE_COLUMN_CARDS, n),
        )
        icon_sz = 620000
        for c, (x, y, w, h) in zip(slide.containers, cells, strict=True):
            shapes.rect(x, y, w, h, t.surface, stroke=t.stroke, rounded=True)
            ty = y + _PAD
            if c.icon is not None:
                shapes.icon(icons.icon_def(c.icon), x + _PAD, ty, icon_sz, t.accent)
                ty += icon_sz + 130000
            shapes.text(x + _PAD, ty, w - 2 * _PAD, y + h - _PAD - ty, _card_paras(c, t))


def _metric_showcase(shapes: ShapeList, slide: Slide, t: PaletteTokens) -> None:
    top = _title_block(shapes, slide, t)
    n = len(slide.containers)
    if n:
        cells = _grid(n, _MX, top + 200000, _CONTENT_W, SLIDE_H - _MY - top - 200000, n)
        icon_sz = 560000
        for c, (x, y, w, h) in zip(slide.containers, cells, strict=True):
            shapes.rect(x, y, w, h, t.surface, stroke=t.stroke, rounded=True)
            ty, th = y + _PAD, h - 2 * _PAD
            if c.icon is not None:
                shapes.icon(
                    icons.icon_def(c.icon), x + (w - icon_sz) // 2, y + _PAD, icon_sz, t.accent
                )
                ty = y + _PAD + icon_sz + 90000
                th = y + h - _PAD - ty
            paras = [
                Para(
                    (
                        Run(
                            c.accent_value or "—",
                            geometry.METRIC_VALUE_SZ,
                            t.accent,
                            t.display_font,
                            bold=True,
                        ),
                    ),
                    align="ctr",
                ),
                Para(
                    (
                        Run(
                            c.heading.upper(),
                            geometry.METRIC_LABEL_SZ,
                            t.muted,
                            t.body_font,
                            bold=True,
                            tracking=200,
                        ),
                    ),
                    align="ctr",
                    space_before=6,
                ),
            ]
            if c.body.strip():
                paras.append(
                    Para(
                        (Run(c.body, geometry.METRIC_BODY_SZ, t.text, t.body_font),),
                        align="ctr",
                        space_before=4,
                    )
                )
            shapes.text(x + _PAD, ty, w - 2 * _PAD, th, paras, anchor="ctr")


def _quote_focus(shapes: ShapeList, slide: Slide, t: PaletteTokens) -> None:
    if slide.slide_title.strip():
        shapes.text(
            _MX,
            450000,
            _CONTENT_W,
            500000,
            [
                Para(
                    (
                        Run(
                            slide.slide_title.upper(),
                            geometry.QUOTE_LABEL_SZ,
                            t.muted,
                            t.body_font,
                            tracking=200,
                        ),
                    )
                )
            ],
        )
    shapes.text(
        _MX,
        1000000,
        2000000,
        1700000,
        [Para((Run("“", 9000, t.accent, t.display_font, bold=True),))],
    )
    quote = slide.containers[0] if slide.containers else Container(body=slide.subtitle)
    shapes.text(
        _MX + 400000,
        2350000,
        _CONTENT_W - 800000,
        2500000,
        [
            Para(
                (
                    Run(
                        quote.body or quote.heading,
                        geometry.QUOTE_TEXT_SZ,
                        t.text,
                        t.display_font,
                        italic=True,
                    ),
                )
            )
        ],
    )
    y = 5100000
    if quote.heading.strip() and quote.body.strip():  # heading doubles as attribution
        shapes.text(
            _MX + 400000,
            y,
            _CONTENT_W - 800000,
            500000,
            [
                Para(
                    (
                        Run(
                            f"— {quote.heading}",
                            geometry.QUOTE_ATTRIBUTION_SZ,
                            t.accent,
                            t.body_font,
                            bold=True,
                        ),
                    )
                )
            ],
        )
        y += 550000
    for extra in slide.containers[1:]:
        line = " · ".join(s for s in (extra.heading, extra.body) if s.strip())
        shapes.text(
            _MX + 400000,
            y,
            _CONTENT_W - 800000,
            420000,
            [Para((Run(line, geometry.QUOTE_LABEL_SZ, t.muted, t.body_font),))],
        )
        y += 450000


def _data_showcase(shapes: ShapeList, slide: Slide, t: PaletteTokens) -> None:
    top = _title_block(shapes, slide, t)
    figure = next((c for c in slide.containers if c.is_figure), None)
    captions = [c for c in slide.containers if not c.is_figure]

    bottom = SLIDE_H - _MY
    if captions:
        caption_h = 950000
        fig_bottom = bottom - caption_h - geometry.GAP
        cells = _grid(len(captions), _MX, bottom - caption_h, _CONTENT_W, caption_h, len(captions))
        for c, (x, y, w, h) in zip(captions, cells, strict=True):
            shapes.rect(x, y, w, h, t.surface, stroke=t.stroke, rounded=True)
            shapes.text(x + _PAD, y + _PAD, w - 2 * _PAD, h - 2 * _PAD, _card_paras(c, t))
    else:
        fig_bottom = bottom

    fig_y = top
    if figure and figure.heading.strip():
        shapes.text(
            _MX,
            fig_y,
            _CONTENT_W,
            450000,
            [
                Para(
                    (Run(figure.heading, geometry.CARD_HEADING_SZ, t.text, t.body_font, bold=True),)
                )
            ],
        )
        fig_y += 500000

    if figure is None:
        return
    fig_h = fig_bottom - fig_y
    if figure.type is ContainerType.TABLE:
        shapes.table(_MX, fig_y, _CONTENT_W, fig_h, figure.rows, _table_style(t))
    elif figure.chart is not None:
        shapes.chart(_MX, fig_y, _CONTENT_W, fig_h, chartpart.build_chart_xml(figure.chart, t))


def _process_flow(shapes: ShapeList, slide: Slide, t: PaletteTokens) -> None:
    top = _title_block(shapes, slide, t)
    steps = slide.containers
    n = len(steps)
    area_top = top + 200000
    area_h = SLIDE_H - _MY - area_top
    arrow_w = 380000
    cell_w = (_CONTENT_W - arrow_w * (n - 1)) // n
    badge, icon_sz = 640000, 460000
    for i, c in enumerate(steps):
        x = _MX + i * (cell_w + arrow_w)
        shapes.rect(x, area_top, cell_w, area_h, t.surface, stroke=t.stroke, rounded=True)
        bx = x + (cell_w - badge) // 2
        shapes.preset(bx, area_top + _PAD, badge, badge, "ellipse", stroke=t.accent, stroke_w=38100)
        shapes.text(
            bx,
            area_top + _PAD,
            badge,
            badge,
            [Para((Run(str(i + 1), 2400, t.accent, t.display_font, bold=True),), align="ctr")],
            anchor="ctr",
        )
        ty = area_top + _PAD + badge + 130000
        if c.icon is not None:
            shapes.icon(icons.icon_def(c.icon), x + (cell_w - icon_sz) // 2, ty, icon_sz, t.accent)
            ty += icon_sz + 90000
        paras: list[Para] = []
        if c.heading.strip():
            paras.append(
                Para(
                    (Run(c.heading, geometry.CARD_HEADING_SZ, t.text, t.body_font, bold=True),),
                    align="ctr",
                )
            )
        if c.body.strip():
            paras.append(
                Para(
                    (Run(c.body, geometry.CARD_BODY_SZ, t.muted, t.body_font),),
                    align="ctr",
                    space_before=4 if paras else 0,
                )
            )
        shapes.text(x + _PAD // 2, ty, cell_w - _PAD, area_top + area_h - _PAD - ty, paras)
        if i < n - 1:
            ax = x + cell_w + (arrow_w - 240000) // 2
            shapes.preset(
                ax, area_top + area_h // 2 - 120000, 240000, 240000, "rightArrow", fill=t.accent
            )


def _comparison(shapes: ShapeList, slide: Slide, t: PaletteTokens) -> None:
    top = _title_block(shapes, slide, t)
    area_top = top + 200000
    area_h = SLIDE_H - _MY - area_top
    gap = 760000
    cw = (_CONTENT_W - gap) // 2
    icon_sz = 620000
    for c, x in ((slide.containers[0], _MX), (slide.containers[1], _MX + cw + gap)):
        shapes.rect(x, area_top, cw, area_h, t.surface, stroke=t.stroke, rounded=True)
        ty = area_top + _PAD
        if c.icon is not None:
            shapes.icon(icons.icon_def(c.icon), x + _PAD, ty, icon_sz, t.accent)
            ty += icon_sz + 130000
        paras: list[Para] = []
        if c.heading.strip():
            paras.append(
                Para(
                    (Run(c.heading, geometry.HERO_SUBTITLE_SZ, t.text, t.display_font, bold=True),)
                )
            )
        if c.body.strip():
            paras.append(
                Para(
                    (Run(c.body, geometry.CARD_BODY_SZ, t.muted, t.body_font),),
                    space_before=6 if paras else 0,
                )
            )
        shapes.text(x + _PAD, ty, cw - 2 * _PAD, area_top + area_h - _PAD - ty, paras)
    vs = 780000
    vx = _MX + cw + (gap - vs) // 2
    vy = area_top + area_h // 2 - vs // 2
    shapes.preset(vx, vy, vs, vs, "ellipse", fill=t.bg, stroke=t.accent, stroke_w=38100)
    shapes.text(
        vx,
        vy,
        vs,
        vs,
        [Para((Run("VS", 2000, t.accent, t.display_font, bold=True),), align="ctr")],
        anchor="ctr",
    )


def _timeline(shapes: ShapeList, slide: Slide, t: PaletteTokens) -> None:
    top = _title_block(shapes, slide, t)
    milestones = slide.containers
    n = len(milestones)
    area_top = top + 200000
    area_h = SLIDE_H - _MY - area_top
    line_y = area_top + area_h // 2
    shapes.rect(_MX, line_y - 20000, _CONTENT_W, 40000, t.accent)
    slot = _CONTENT_W // n
    dot, box_h = 210000, area_h // 2 - 340000
    for i, c in enumerate(milestones):
        cx = _MX + slot * i + slot // 2
        shapes.preset(cx - dot // 2, line_y - dot // 2, dot, dot, "ellipse", fill=t.accent)
        paras: list[Para] = []
        if c.heading.strip():
            paras.append(
                Para(
                    (Run(c.heading, geometry.CARD_HEADING_SZ, t.text, t.body_font, bold=True),),
                    align="ctr",
                )
            )
        if c.body.strip():
            paras.append(
                Para(
                    (Run(c.body, geometry.CARD_BODY_SZ, t.muted, t.body_font),),
                    align="ctr",
                    space_before=4 if paras else 0,
                )
            )
        tx, tw = _MX + slot * i + 90000, slot - 180000
        if i % 2 == 0:
            shapes.text(tx, area_top, tw, box_h, paras, anchor="b")
        else:
            shapes.text(tx, line_y + 320000, tw, box_h, paras, anchor="t")


def _image_feature(shapes: ShapeList, slide: Slide, t: PaletteTokens) -> None:
    image = next((c for c in slide.containers if c.is_image), None)
    captions = [c for c in slide.containers if not c.is_image]

    left_w = 5200000
    # Text column (left): accent bar, title, subtitle, caption cards.
    shapes.rect(_MX, 900000, 900000, 76200, t.accent)
    shapes.text(
        _MX,
        1150000,
        left_w,
        1800000,
        [
            Para(
                (
                    Run(
                        slide.slide_title,
                        geometry.SPLIT_TITLE_SZ,
                        t.text,
                        t.display_font,
                        bold=True,
                    ),
                )
            )
        ],
    )
    y = 3050000
    if slide.subtitle.strip():
        shapes.text(
            _MX,
            y,
            left_w,
            1200000,
            [Para((Run(slide.subtitle, geometry.SUBTITLE_SZ, t.muted, t.body_font),))],
        )
        y += 700000
    for c in captions:
        if not (c.heading.strip() or c.body.strip()):
            continue
        shapes.text(_MX, y, left_w, 900000, _card_paras(c, t))
        y += 950000

    # Image column (right): the generated picture, or a styled placeholder.
    img_x = _MX + left_w + 400000
    img_w = SLIDE_W - _MX - img_x
    img_y, img_h = _MY, SLIDE_H - 2 * _MY
    if image is not None and image.image_bytes:
        shapes.picture(img_x, img_y, img_w, img_h, image.image_bytes)
    else:
        # No image yet (generation off/failed): a clean accent-tinted panel with
        # the caption, so the deck still renders and reads intentionally.
        shapes.rect(img_x, img_y, img_w, img_h, t.surface, stroke=t.stroke, rounded=True)
        shapes.rect(img_x, img_y, img_w, 90000, t.accent, rounded=True)
        label = (image.heading if image else "").strip() or "Image"
        shapes.text(
            img_x + _PAD,
            img_y + img_h // 2 - 300000,
            img_w - 2 * _PAD,
            600000,
            [
                Para(
                    (Run(label, geometry.CARD_HEADING_SZ, t.muted, t.body_font, bold=True),),
                    align="ctr",
                )
            ],
            anchor="ctr",
        )


_LAYOUTS = {
    LayoutType.TITLE_HERO: _title_hero,
    LayoutType.SPLIT_GRID: _split_grid,
    LayoutType.THREE_COLUMN_CARDS: _column_cards,
    LayoutType.METRIC_SHOWCASE: _metric_showcase,
    LayoutType.QUOTE_FOCUS: _quote_focus,
    LayoutType.DATA_SHOWCASE: _data_showcase,
    LayoutType.PROCESS_FLOW: _process_flow,
    LayoutType.COMPARISON: _comparison,
    LayoutType.TIMELINE: _timeline,
    LayoutType.IMAGE_FEATURE: _image_feature,
}


def render_slide(slide: Slide, tokens: PaletteTokens) -> RenderedSlide:
    """One validated slide → its slideN.xml part plus any embedded chart
    parts. Decor is drawn first (behind the content); pure and deterministic."""
    shapes = ShapeList()
    decor.apply(shapes, tokens, slide.layout_type)
    _LAYOUTS[slide.layout_type](shapes, slide, tokens)
    return RenderedSlide(xml=slide_part(shapes, tokens.bg), rels=shapes.rels)


def render_deck(deck: Deck, *, tokens: PaletteTokens | None = None) -> bytes:
    """A validated Deck (from schema.validate_deck) → .pptx bytes.
    Rendering twice yields byte-identical output (fixed zip timestamps).

    `tokens`, when given, overrides the deck's chosen built-in palette —
    for a workspace's brand-derived tokens (brand_extract.py). The model
    still only ever picks a closed `Palette` enum value; this override
    is applied by the caller (orchestration layer), never by the model."""
    active = tokens or PALETTES[deck.palette]
    return package([render_slide(slide, active) for slide in deck.slides])
