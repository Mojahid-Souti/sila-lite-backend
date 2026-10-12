"""Clone-and-fill rendering against a DESIGNED corporate .pptx.

Why this exists, and why it is not `render.py` with different colours:

`render.py` draws slides from scratch and `brand_extract.py` recolours them from
a template's THEME. That is the right pipeline for a template whose branding
lives in its master and layouts. It is the wrong one for a template like
Omantel's, where the layouts are stock Office defaults, the theme is stock
Office 2024, and every brand mark — the logo, the header bar, the blue arrow,
the orange diamonds — is a hand-placed shape on a slide sitting on the "Blank"
layout. Generating "against" those layouts inherits nothing, which is exactly
why a deck built that way looked nothing like the template.

So: take a DESIGNED slide as a stencil, keep every shape, image, colour and
coordinate, and rewrite only its text. Anything the deck does not fill is
deleted rather than left holding the template's lorem ipsum.

Complexity: O(total shapes across the emitted slides); each is visited once.
"""

from __future__ import annotations

import copy
import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from sila.services.slides.deck_guard import (
    MIN_BODY_PT,
    Expectation,
    prune_orphans,
    validate_package,
)
from sila.services.slides.identity import refresh_identity
from sila.services.slides.rels import allocate_rel_ids
from sila.services.slides.tokens import PaletteTokens

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
for prefix, uri in (("a", A), ("p", P), ("r", R)):
    ET.register_namespace(prefix, uri)

_SLIDE_CT = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
_SLIDE_REL = f"{R}/slide"

EMU_PER_INCH = 914400


class TemplateFillError(RuntimeError):
    """The template does not have the shape this filler assumes."""


# --- what a caller asks for -------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One card: a bold label and its sentence.

    Both, always — a group of cards where some have a label and some are a bare
    fragment is the "inconsistent card structure" defect. `body` may be empty
    only for a stencil whose slot is a single line by design.
    """

    label: str
    body: str = ""


@dataclass(frozen=True)
class Slide:
    kind: str
    title: str = ""
    subtitle: str = ""
    slots: tuple[Slot, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    # Comparison-table options, both opt-in so an ordinary table is unchanged.
    # A cell value containing "\n" becomes a bold first line and a plain second.
    # `table_highlight_col` tints one column (1-based) to mark the subject of a
    # comparison; `table_center_from` centres that column and everything right
    # of it, because a column of ticks reads as a column, not as prose.
    table_highlight_col: int = 0
    table_center_from: int = 0
    # A validated `schema.ChartSpec`. Rendered as a NATIVE chart part so the
    # numbers stay editable in PowerPoint rather than being a picture.
    chart: object | None = None
    # Copy that did not fit the card, kept for the presenter instead of lost.
    notes: str = ""
    # An `imagery.ImageAsset`. Embedded as BYTES, never linked: a linked picture
    # breaks the moment the deck is emailed or the URL moves, and this deck has
    # to render with no network at all.
    image: object | None = None


# --- what a template offers -------------------------------------------------


@dataclass(frozen=True)
class Stencil:
    """One designed slide, and where its text lives.

    Indices are into the slide's `<p:sp>` elements in document order (groups
    included, since a stencil's shapes are often grouped).
    """

    source: int
    title_sp: int | None = None
    subtitle_sp: int | None = None
    slot_sps: tuple[int, ...] = ()
    header_sps: tuple[int, int] | None = None  # (deck name, year)
    table: bool = False
    # Replace the stencil's table with a native chart frame in the same box.
    chart: bool = False
    # A stencil with no title of its own borrows one: (source slide, shape) and
    # the inches to place it at. Keeps the type styling authentic.
    title_from: tuple[int, int] | None = None
    title_at: tuple[float, float] = (0.0, 0.0)
    # Inches the title box is widened to, and the character count past which the
    # type scales down. Both exist because a stencil's box is sized for its own
    # placeholder, not for a real headline.
    title_size: tuple[float, float] | None = None
    title_chars: int = 0
    # Characters a card carries at its drawn point size; past this the card's
    # type scales down so the sentence still FINISHES.
    slot_chars: int = 0
    # Point size for a slot's label when the label is the point of the slide —
    # a KPI figure, not a heading over a sentence.
    big_label_pt: int = 0
    # Where a picture may go on this stencil, in inches. Absent means the
    # stencil has no free region, and an image is SKIPPED rather than floated
    # over the designed content.
    image_box: tuple[float, float, float, float] | None = None
    # Where a one-line subtitle sits, cloned from the title shape and set
    # smaller and muted. A heading followed straight by raw data gives the
    # reader no framing, which is what separates a draft from a finished deck.
    # A decorative shape to relocate, as (near_x, near_y, to_x, to_y) in inches.
    # The stencil parks its spark diamond beside a title that sat centre-right;
    # moving the title to the left margin (the brand's own hierarchy) leaves the
    # spark sitting on top of it, so the spark moves with it — as the marker the
    # guide describes rather than as an obstacle.
    move_spark: tuple[float, float, float, float] | None = None
    subtitle_at: tuple[float, float] | None = None
    subtitle_size: tuple[float, float] = (9.60, 0.42)
    # Uniform slot geometry. The stencil's own slots differ in height (one is
    # 1.16in against its siblings' 1.40in), which a large figure overflows —
    # that is how a KPI descriptor went missing on exactly one position.
    # Explicit boxes for the slots, in visual order. The stencil's slots carry
    # NO xfrm of their own — they inherit their geometry — so resizing them is a
    # no-op and one inherited box is shorter than its siblings, which is what
    # clipped a KPI descriptor off the slide. Writing an xfrm takes control.
    slot_boxes: tuple[tuple[float, float, float, float], ...] = ()
    # Forced colour for a slot's descriptor. One of the stencil's four columns
    # carries WHITE body text, inherited from a layout where it sat on a
    # coloured field — invisible on a white slide, and invisible is worse than
    # wrong because nothing looks broken.
    slot_body_colour: str = ""
    # Decorative shapes that are NOT text: the generic tap icon the stencil
    # repeats in every diamond. Always removed — the same icon four times says
    # nothing, and one column carrying a different icon (a house) reads as a
    # mistake. The diamond frames stay; they are the design.
    icon_sps: tuple[int, ...] = ()
    # Per slot position, the frame + connector shapes that belong to it, so an
    # unused slot takes its empty diamond with it instead of leaving a blank
    # frame at the end of the chain.
    slot_decor: tuple[tuple[int, ...], ...] = ()
    max_slots: int = field(default=0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_slots", len(self.slot_sps))


# The brand's own colours, read off its drawn shapes (the theme carries stock
# Office). A chart never invents a colour; it draws from these.
OMANTEL_TOKENS = PaletteTokens(
    bg="FFFFFF",
    surface="F4F4F8",
    stroke="D8D8E4",
    text="1D1D1D",
    muted="6B6B78",
    accent="2D13EA",
    display_font="FS Alvar",
    body_font="FS Alvar",
)
# Orange is the highlight, used for the one figure a slide wants noticed.
OMANTEL_HIGHLIGHT = "FF7800"

# The brand has TWO typefaces and they are script-specific: FS Alvar covers
# Latin only, FS Albert Arabic covers Arabic. A run therefore needs both set —
# `a:latin` for Latin and `a:cs` for complex script — or Arabic silently falls
# back to whatever the reader's machine offers and the layout changes with it.
LATIN_FONT = "FS Alvar"
ARABIC_FONT = "FS Albert Arabic"
_LATIN_TAG = f"{{{A}}}latin"
_CS_TAG = f"{{{A}}}cs"


# The template's own artwork was drawn at #2E12EB — one step off Deep Sea. Close
# enough to look right, wrong enough to fail a brand check, and a deck that ships
# an approximation of the signature colour is not on-brand.
_EXACT_BRAND = (OMANTEL_TOKENS.accent, OMANTEL_HIGHLIGHT, "1D1D1D")
_COLOUR_SNAP = 8  # max per-channel distance treated as "meant to be this"


def _channels(value: str) -> tuple[int, int, int] | None:
    if len(value) != 6:
        return None
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        return None


def normalise_brand_colours(root: ET.Element) -> int:
    """Snap near-miss brand colours to their exact token values.

    Deliberately narrow: only a colour already within `_COLOUR_SNAP` of a brand
    value moves, so this corrects a drafting drift and never repaints anything
    the designer chose on purpose.
    """
    corrected = 0
    for node in root.iter():
        value = node.get("val")
        if not value or not node.tag.endswith("}srgbClr"):
            continue
        source = _channels(value.upper())
        if source is None:
            continue
        for target in _EXACT_BRAND:
            wanted = _channels(target)
            if wanted is None or value.upper() == target:
                continue
            if all(abs(a - b) <= _COLOUR_SNAP for a, b in zip(source, wanted, strict=True)):
                node.set("val", target)
                corrected += 1
                break
    return corrected


def _apply_brand_fonts(shape: ET.Element) -> None:
    """Set the Latin and complex-script faces on every run of a shape we filled.

    Only on OUR text: the template's own chrome already carries the designer's
    choices, and rewriting those would be editing the artwork rather than
    filling it.
    """
    for props in shape.iter():
        if props.tag not in (f"{{{A}}}rPr", f"{{{A}}}defRPr", f"{{{A}}}endParaRPr"):
            continue
        for tag, face in ((_LATIN_TAG, LATIN_FONT), (_CS_TAG, ARABIC_FONT)):
            node = props.find(tag)
            if node is None:
                node = ET.SubElement(props, tag)
            node.set("typeface", face)


# Measured from the file, not guessed. See templates/README.md.
OMANTEL: dict[str, Stencil] = {
    "title": Stencil(
        source=1,
        title_sp=0,
        header_sps=(1, 2),
        # The stencil's box overlaps the light-trail graphic once a headline
        # wraps to a third line; a wider, higher box keeps the type clear of it.
        title_at=(1.60, 0.55),
        title_size=(10.00, 1.50),
        title_chars=30,
    ),
    # The section divider is the one stencil with a genuinely empty right half,
    # which is why it is the only one that takes a picture. A section transition
    # is also where an image earns its place in a data review.
    "section": Stencil(
        source=2,
        title_sp=7,
        header_sps=(0, 1),
        title_chars=34,
        subtitle_at=(0.90, 1.88),
        subtitle_size=(7.40, 0.60),
        image_box=(6.85, 2.70, 4.90, 3.20),
    ),
    "statement": Stencil(source=11, title_sp=13, title_chars=40),
    "points": Stencil(
        subtitle_at=(1.05, 3.56),
        source=8,
        title_sp=16,
        slot_body_colour=OMANTEL_TOKENS.text,
        slot_sps=(9, 10, 11),
        # sp19 is a filled house icon in the first diamond, sp14/15 the tap
        # icon in the other two — measured from slide8.xml.
        icon_sps=(19, 14, 15),
        slot_decor=((0,), (1, 12), (2, 13, 17)),
        header_sps=(4, 5),
        slot_chars=150,
        title_size=(4.40, 1.60),
        title_chars=30,
    ),
    "points4": Stencil(
        subtitle_at=(0.88, 2.25),
        source=7,
        # sp14 is ONE shape holding all four tap icons (16 paths); the diamonds
        # sp5/7/6/8 and connectors sp13/9/11/10/12 are separate — slide7.xml.
        icon_sps=(14,),
        slot_decor=((5, 13), (7, 9), (6, 11), (8, 10, 12)),
        slot_body_colour=OMANTEL_TOKENS.text,
        slot_sps=(15, 17, 16, 18),
        header_sps=(0, 1),
        slot_chars=120,
        title_from=(8, 16),
        title_at=(0.88, 1.35),
        title_size=(5.60, 0.90),
        title_chars=30,
    ),
    # A chart reuses the table slide's furniture — same title, same header bar,
    # same clear middle — with the table swapped for a native chart frame.
    "chart": Stencil(
        subtitle_at=(0.62, 1.72),
        source=9,
        title_sp=0,
        header_sps=(1, 2),
        table=True,
        chart=True,
        title_at=(5.15, 1.24),
        title_size=(6.40, 0.80),
        title_chars=26,
    ),
    # Numbers read as numbers: the four-across stencil with its label slot blown
    # up, so "25" dominates and its descriptor sits beneath.
    "kpi": Stencil(
        subtitle_at=(0.88, 2.25),
        source=7,
        # sp14 is ONE shape holding all four tap icons (16 paths); the diamonds
        # sp5/7/6/8 and connectors sp13/9/11/10/12 are separate — slide7.xml.
        icon_sps=(14,),
        slot_decor=((5, 13), (7, 9), (6, 11), (8, 10, 12)),
        slot_sps=(15, 17, 16, 18),
        header_sps=(0, 1),
        title_from=(8, 16),
        title_at=(0.88, 1.35),
        title_size=(5.60, 0.90),
        title_chars=30,
        slot_chars=110,
        big_label_pt=8000,
        slot_body_colour=OMANTEL_TOKENS.text,
        # The four columns the stencil draws, given explicit height so an 80pt
        # figure and its descriptor both fit.
        # Below the diamond chain, which ends at y≈4.66: at 4.15 an 80pt figure's
        # cap height reached up into the frames.
        slot_boxes=(
            (0.88, 4.80, 2.60, 2.00),
            (3.69, 4.80, 2.60, 2.00),
            (6.59, 4.80, 2.60, 2.00),
            (9.61, 4.80, 2.60, 2.00),
        ),
    ),
    "table": Stencil(
        subtitle_at=(0.62, 1.72),
        source=9,
        title_sp=0,
        header_sps=(1, 2),
        table=True,
        # The stencil parks two spark diamonds at x=11.16 and x=11.74, y=6.0+ —
        # inside the footprint of a table that spans 0.62in to 12.65in. They sit
        # ON the last column of any table tall enough to reach them, which is
        # every table of more than four rows. The title-side spark (index 3) is
        # kept and relocated by move_spark below; these two go.
        icon_sps=(4, 5),
        # Clear of the diamond the stencil parks at x=4.47, which a headline
        # longer than its "Table Statement" placeholder wrapped straight into.
        move_spark=(4.47, 1.28, 0.10, 1.14),
        title_at=(0.72, 1.02),
        title_size=(8.60, 0.76),
        title_chars=34,
    ),
}


# --- XML helpers ------------------------------------------------------------


def _q(tag: str) -> str:
    prefix, local = tag.split(":")
    return f"{{{A if prefix == 'a' else P if prefix == 'p' else R}}}{local}"


def _shapes(root: ET.Element) -> list[ET.Element]:
    """Every `<p:sp>` in document order — the numbering the stencils index."""
    return list(root.iter(_q("p:sp")))


def _parents(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def _set_paragraph(para: ET.Element, text: str) -> None:
    """Write `text` into a paragraph, keeping the FIRST run's formatting.

    The template splits a line across many runs ('Presentation' / ' ' / 'name'),
    so a naive replace of the joined string matches nothing and fails silently.
    Keeping run 0 keeps its size, weight and colour; the rest go, along with the
    manual line breaks that only made sense for the original wording.
    """
    runs = para.findall(_q("a:r"))
    if not runs:
        return
    first, *rest = runs
    node = first.find(_q("a:t"))
    if node is None:
        node = ET.SubElement(first, _q("a:t"))
    node.text = text
    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for run in rest:
        para.remove(run)
    for br in para.findall(_q("a:br")):
        para.remove(br)


def _fill_shape(shape: ET.Element, *lines: str) -> None:  # noqa: D401
    """Fill a shape's paragraphs in order; drop the paragraphs left over.

    A stencil card is `p0` = bold label, `p1` = body. Passing one line to a
    two-paragraph card removes the body rather than leaving lorem ipsum under
    the new label.
    """
    body = shape.find(_q("p:txBody"))
    if body is None:
        return
    paras = body.findall(_q("a:p"))
    for para, text in zip(paras, lines, strict=False):
        _set_paragraph(para, text)
    for para in paras[len(lines) :]:
        body.remove(para)
    _apply_brand_fonts(shape)


def _drop(shape: ET.Element, parents: dict[ET.Element, ET.Element]) -> None:
    parent = parents.get(shape)
    if parent is not None:
        parent.remove(shape)


def _move(shape: ET.Element, x_in: float, y_in: float) -> None:
    off = shape.find(f"./{_q('p:spPr')}/{_q('a:xfrm')}/{_q('a:off')}")
    if off is not None:
        off.set("x", str(int(x_in * EMU_PER_INCH)))
        off.set("y", str(int(y_in * EMU_PER_INCH)))


def _fill_table(
    root: ET.Element,
    rows: tuple[tuple[str, ...], ...],
    *,
    highlight_col: int = 0,
    center_from: int = 0,
) -> None:
    """Rewrite the stencil table: header row keeps its brand fill, body rows are
    cloned from the stencil's own second row so banding and borders survive."""
    tbl = next(root.iter(_q("a:tbl")), None)
    if tbl is None:
        return
    if not rows:
        # An unfilled stencil table is NOT an empty table — it still holds the
        # template's sample rows, which is how "+900 / 100,000 users / August 12
        # 2011" shipped under a heading of ours. Delete the frame instead.
        _drop_frame(root, tbl)
        return
    trs = tbl.findall(_q("a:tr"))
    if len(trs) < 2:
        raise TemplateFillError("table stencil needs a header and one body row")
    header, sample = trs[0], trs[1]
    width = _trim_columns(tbl, trs, len(rows[0]))

    def write(tr: ET.Element, values: tuple[str, ...]) -> None:
        for cell, value in zip(tr.findall(_q("a:tc")), values, strict=False):
            _fill_shape_like(cell, value)

    write(header, rows[0][:width])
    for tr in trs[1:]:
        tbl.remove(tr)
    for values in rows[1:]:
        clone = copy.deepcopy(sample)
        write(clone, values[:width])
        for cell in clone.findall(_q("a:tc"))[len(values) :]:
            _fill_shape_like(cell, "")
        tbl.append(clone)
    _style_table(tbl, _TABLE_W, highlight_col=highlight_col, center_from=center_from)
    _widen_frame(root, tbl, _TABLE_X, _TABLE_Y, _TABLE_W)


# A PLACEHOLDER, swapped for a conforming rId<n> when the rels are written.
_IMAGE_RID = "SILA-IMAGE-RID"
_IMAGE_REL_TYPE = f"{R}/image"


def _place_image(root: ET.Element, asset: object, box: tuple[float, float, float, float]) -> None:
    """Drop a picture into a defined panel, cropped to fit — never stretched.

    `a:srcRect` crops in the picture's own coordinate space, so the box governs
    the region and the image keeps its proportions. Stretching to the box is the
    thing that makes a deck look automated.
    """
    width = int(getattr(asset, "width", 0) or 1)
    height = int(getattr(asset, "height", 0) or 1)
    x_in, y_in, w_in, h_in = box
    box_aspect = w_in / h_in
    src_aspect = width / height
    if src_aspect > box_aspect:  # source is wider — trim the sides
        keep = box_aspect / src_aspect
        inset = int((1 - keep) / 2 * 100000)
        crop = f'<a:srcRect l="{inset}" r="{inset}"/>'
    else:  # source is taller — trim top and bottom
        keep = src_aspect / box_aspect
        inset = int((1 - keep) / 2 * 100000)
        crop = f'<a:srcRect t="{inset}" b="{inset}"/>'

    tree = root.find(f".//{_q('p:spTree')}")
    if tree is None:
        return
    tree.append(
        ET.fromstring(
            f'<p:pic xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
            '<p:nvPicPr><p:cNvPr id="950" name="Image"/>'
            '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
            f'<p:blipFill><a:blip r:embed="{_IMAGE_RID}"/>{crop}'
            "<a:stretch><a:fillRect/></a:stretch></p:blipFill>"
            f'<p:spPr><a:xfrm><a:off x="{Emu(x_in)}" y="{Emu(y_in)}"/>'
            f'<a:ext cx="{Emu(w_in)}" cy="{Emu(h_in)}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
        )
    )


_CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_CHART_CT = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
_CHART_REL_TYPE = f"{R}/chart"
# rId1..n on a cloned slide already belong to the layout and its images, so the
# chart takes an id far outside that range rather than guessing the next free.
# A PLACEHOLDER, swapped for a conforming rId<n> when the rels are written.
_CHART_RID = "SILA-CHART-RID"


def _swap_table_for_chart(root: ET.Element, spec: object) -> str:
    """Put a native chart where the stencil drew its table.

    Reusing the table's own frame keeps the chart inside the designed layout —
    same margins, same relationship to the header rule and the diamonds — and a
    real `c:chart` part means the numbers stay editable in PowerPoint instead of
    being a picture of a chart.
    """
    from sila.services.slides import chartpart

    tbl = next(root.iter(_q("a:tbl")), None)
    box = (Emu(1.60), Emu(2.05), Emu(9.90), Emu(4.30))
    if tbl is not None:
        parents = _parents(root)
        frame: ET.Element | None = tbl
        while frame is not None and frame.tag != _q("p:graphicFrame"):
            frame = parents.get(frame)
        if frame is not None:
            off = frame.find(f"./{_q('p:xfrm')}/{_q('a:off')}")
            # The stencil's table box leaves the bottom third of the slide
            # empty; a chart earns that space, so only its ORIGIN is reused.
            if off is not None:
                box = (int(off.get("x", "0")) - Emu(0.9), int(off.get("y", "0")), *box[2:])
            _drop_frame(root, tbl)

    x, y, w, h = box
    tree = root.find(f".//{_q('p:spTree')}")
    if tree is None:
        raise TemplateFillError("slide has no shape tree")
    tree.append(
        ET.fromstring(
            "<p:graphicFrame "
            f'xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
            '<p:nvGraphicFramePr><p:cNvPr id="900" name="Chart"/>'
            "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
            f'<p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></p:xfrm>'
            f'<a:graphic><a:graphicData uri="{_CHART_NS}">'
            f'<c:chart xmlns:c="{_CHART_NS}" r:id="{_CHART_RID}"/>'
            "</a:graphicData></a:graphic></p:graphicFrame>"
        )
    )
    raw = chartpart.build_chart_xml(spec, OMANTEL_TOKENS)  # type: ignore[arg-type]
    return _style_chart(raw)


_CQ = f"{{{_CHART_NS}}}"


# Four on-brand colours: the brand blue, its darker and lighter tints, and the
# orange. One per bar, so a legend can name them.
CHART_SERIES_COLORS = ("2D13EA", "FF7800", "6A57F0", "A8AEC9")
# Projected, not read on a laptop.
_CHART_LABEL_PT = 1400
_CHART_AXIS_PT = 1300
_CHART_LEGEND_PT = 1500


def _style_chart(chart_xml: str) -> str:
    """Add value labels, and paint the leading point in the brand's orange.

    `chartpart` draws a clean chart but a bare one — no data labels, and one
    colour per series. On a slide a reader glances at, the number belongs ON the
    bar, and the figure the slide is about should be the one that stands out.

    DrawingML fixes the order inside `<c:ser>` (… dPt*, dLbls, cat, val …), so
    both are inserted before `<c:cat>` rather than appended.
    """
    ET.register_namespace("c", _CHART_NS)
    root = ET.fromstring(chart_xml)
    for ser in root.iter(f"{_CQ}ser"):
        children = list(ser)
        anchor = next(
            (i for i, node in enumerate(children) if node.tag in (f"{_CQ}cat", f"{_CQ}val")),
            len(children),
        )
        # One colour per bar so the legend can name each state, rather than a
        # single highlight against three identical blues.
        points = len(ser.findall(f".//{_CQ}cat//{_CQ}pt")) or len(CHART_SERIES_COLORS)
        for position in reversed(range(min(points, 12))):
            colour = CHART_SERIES_COLORS[position % len(CHART_SERIES_COLORS)]
            ser.insert(
                anchor,
                ET.fromstring(
                    f'<c:dPt xmlns:c="{_CHART_NS}" xmlns:a="{A}"><c:idx val="{position}"/>'
                    '<c:invertIfNegative val="0"/><c:bubble3D val="0"/>'
                    f'<c:spPr><a:solidFill><a:srgbClr val="{colour}"/></a:solidFill></c:spPr>'
                    "</c:dPt>"
                ),
            )
        highlight = None
        labels = ET.fromstring(
            f'<c:dLbls xmlns:c="{_CHART_NS}" xmlns:a="{A}">'
            "<c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>"
            f'<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="{_CHART_LABEL_PT}" b="1">'
            f'<a:solidFill><a:srgbClr val="{OMANTEL_TOKENS.text}"/></a:solidFill>'
            f'<a:latin typeface="{OMANTEL_TOKENS.body_font}"/>'
            '</a:defRPr></a:pPr><a:endParaRPr lang="en-US"/></a:p></c:txPr>'
            '<c:showLegendKey val="0"/><c:showVal val="1"/><c:showCatName val="0"/>'
            '<c:showSerName val="0"/><c:showPercent val="0"/><c:showBubbleSize val="0"/>'
            "</c:dLbls>"
        )
        # Twelve labels on a line chart collide with the line and each other.
        # Past eight points, keep the three a reader actually uses — the first,
        # the peak, and the last — and delete the rest per point.
        values = [
            float(v.text or "nan")
            for v in ser.findall(f".//{_CQ}val//{_CQ}pt/{_CQ}v")
            if (v.text or "").strip()
        ]
        if len(values) > 8:
            peak = max(range(len(values)), key=lambda i: values[i])
            keep = {0, peak, len(values) - 1}
            for position in reversed(range(len(values))):
                if position in keep:
                    continue
                labels.insert(
                    0,
                    ET.fromstring(
                        f'<c:dLbl xmlns:c="{_CHART_NS}"><c:idx val="{position}"/>'
                        '<c:delete val="1"/></c:dLbl>'
                    ),
                )
        ser.insert(anchor, labels)
        if highlight is not None:
            ser.insert(anchor, highlight)

    _enlarge_chart_text(root)
    _name_each_bar(root)
    return ET.tostring(root, encoding="unicode")


# A category name longer than this reads badly on a vertical axis — the labels
# either angle or wrap. Horizontal bars give each name a full row.
_LONG_CATEGORY_CHARS = 14


def _name_each_bar(root: ET.Element) -> None:
    """Vary the colour PER POINT and give the chart a legend that names them.

    A single-series bar chart emits no legend, because DrawingML legends list
    series. `varyColors` is what makes a reader's PowerPoint list the points
    instead, which is the only way four differently-coloured bars can be
    identified without relying on the axis alone.
    """
    labels = [node.text or "" for cat in root.iter(f"{_CQ}cat") for node in cat.iter(f"{_CQ}v")]
    for bar in root.iter(f"{_CQ}barChart"):
        vary = bar.find(f"{_CQ}varyColors")
        if vary is not None:
            vary.set("val", "1")
        direction = bar.find(f"{_CQ}barDir")
        if direction is not None and any(len(v) > _LONG_CATEGORY_CHARS for v in labels):
            # Long state names ("Published and available") need a full row.
            direction.set("val", "bar")

    chart = next(root.iter(f"{_CQ}chart"), None)
    if chart is None or chart.find(f"{_CQ}legend") is not None:
        return
    children = list(chart)
    plot_area = next(
        (i for i, node in enumerate(children) if node.tag == f"{_CQ}plotArea"), len(children) - 1
    )
    chart.insert(
        plot_area + 1,
        ET.fromstring(
            f'<c:legend xmlns:c="{_CHART_NS}" xmlns:a="{A}"><c:legendPos val="b"/>'
            '<c:overlay val="0"/>'
            f"<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr>"
            f'<a:defRPr sz="{_CHART_LEGEND_PT}">'
            f'<a:solidFill><a:srgbClr val="{OMANTEL_TOKENS.muted}"/></a:solidFill>'
            f'<a:latin typeface="{OMANTEL_TOKENS.body_font}"/>'
            '</a:defRPr></a:pPr><a:endParaRPr lang="en-US"/></a:p></c:txPr></c:legend>'
        ),
    )


def _enlarge_chart_text(root: ET.Element) -> None:
    """Push every default run size up to something projectable.

    `chartpart` sizes for a dense generated slide; on a template slide the chart
    owns the page, and 8pt axis labels are unreadable from the back of a room.
    """
    for axis_tag in (f"{_CQ}catAx", f"{_CQ}valAx"):
        for axis in root.iter(axis_tag):
            for node in axis.iter():
                if node.tag == f"{{{A}}}defRPr":
                    node.set("sz", str(_CHART_AXIS_PT))
    for legend in root.iter(f"{_CQ}legend"):
        for node in legend.iter():
            if node.tag == f"{{{A}}}defRPr":
                node.set("sz", str(_CHART_LEGEND_PT))


def Emu(inches: float) -> int:  # noqa: N802 — reads as a unit, not a class
    return int(inches * EMU_PER_INCH)


# The stencil parks its table in the middle third; a real table earns the
# content area, which is what leaves no cramped block above a blank half-slide.
_TABLE_X = 570000
_TABLE_Y = 2360000
_TABLE_W = 11000000


def _widen_frame(root: ET.Element, node: ET.Element, x: int, y: int, w: int) -> None:
    parents = _parents(root)
    frame: ET.Element | None = node
    while frame is not None and frame.tag != _q("p:graphicFrame"):
        frame = parents.get(frame)
    if frame is None:
        return
    off = frame.find(f"./{_q('p:xfrm')}/{_q('a:off')}")
    ext = frame.find(f"./{_q('p:xfrm')}/{_q('a:ext')}")
    if off is not None:
        off.set("x", str(x))
        off.set("y", str(y))
    if ext is not None:
        ext.set("cx", str(w))
    tbl_w = node.find(f"./{_q('a:tblPr')}")
    if tbl_w is not None:
        tbl_w.set("firstRow", "1")


def _drop_frame(root: ET.Element, node: ET.Element) -> None:
    """Remove the graphicFrame that owns `node`, not just the node."""
    parents = _parents(root)
    current: ET.Element | None = node
    while current is not None:
        parent = parents.get(current)
        if parent is not None and parent.tag == _q("p:graphicFrame"):
            grandparent = parents.get(parent)
            if grandparent is not None:
                grandparent.remove(parent)
            return
        current = parent


def _trim_columns(tbl: ET.Element, trs: list[ET.Element], wanted: int) -> int:
    """Fit the stencil table to the number of columns the data actually has.

    Narrowing came first: a 5-column stencil filled with 3 columns of data
    otherwise publishes the template's spare headers as if they were real.

    Widening was added for comparison tables, which need one column per thing
    compared plus one for the feature — six against this stencil's five. A new
    column is CLONED from the last one so it inherits the stencil's borders,
    fill and run properties; building one from scratch would be the only cell
    in the table not drawn by the designer.
    """
    grid = tbl.find(_q("a:tblGrid"))
    if grid is None:
        return len(trs[0].findall(_q("a:tc")))
    cols = grid.findall(_q("a:gridCol"))
    if wanted > len(cols):
        for _ in range(wanted - len(cols)):
            grid.append(copy.deepcopy(cols[-1]))
        for tr in trs:
            cells = tr.findall(_q("a:tc"))
            for _ in range(wanted - len(cells)):
                tr.append(copy.deepcopy(cells[-1]))
        return wanted
    keep = max(1, min(wanted, len(cols)))
    if keep == len(cols):
        return keep
    total = sum(int(c.get("w", "0")) for c in cols)
    for col in cols[keep:]:
        grid.remove(col)
    for col in cols[:keep]:
        col.set("w", str(total // keep))
    for tr in trs:
        for cell in tr.findall(_q("a:tc"))[keep:]:
            tr.remove(cell)
    return keep


# The header name box, at minimum, so a deck title stays on one line.
# Wide enough for a real deck title on one line. 2.60 fit "Presentation name"
# and wrapped "Retention Pack — Weekly Escalations & Performance" onto two.
_HEADER_MIN_IN = 5.40
_HEADER_CHARS = 52


def _clip_header(name: str) -> str:
    """The running header is a label, not a sentence; past the box it wraps."""
    text = " ".join(name.split())
    if len(text) <= _HEADER_CHARS:
        return text
    return text[: _HEADER_CHARS - 1].rstrip(" -—–,;:") + "…"


def _widen(shape: ET.Element, w_in: float) -> None:
    """Widen a box about its own centre, leaving its vertical geometry alone."""
    xfrm = shape.find(f"./{_q('p:spPr')}/{_q('a:xfrm')}")
    if xfrm is None:
        return
    off, ext = xfrm.find(_q("a:off")), xfrm.find(_q("a:ext"))
    if off is None or ext is None:
        return
    current = int(ext.get("cx", "0"))
    wanted = int(w_in * EMU_PER_INCH)
    if current >= wanted:
        return
    off.set("x", str(int(off.get("x", "0")) - (wanted - current) // 2))
    ext.set("cx", str(wanted))


def _resize(shape: ET.Element, w_in: float, h_in: float) -> None:
    ext = shape.find(f"./{_q('p:spPr')}/{_q('a:xfrm')}/{_q('a:ext')}")
    if ext is not None:
        ext.set("cx", str(int(w_in * EMU_PER_INCH)))
        ext.set("cy", str(int(h_in * EMU_PER_INCH)))


def _shrink_to_fit(shape: ET.Element, text: str, budget: int) -> None:
    """Scale a headline down when it runs past what the stencil was drawn for.

    The stencil's title box is sized for its own placeholder ("Timeline Goes
    here"); real titles are longer, and at the stencil's point size they either
    wrap into the artwork or overflow the slide. Scaling is deterministic and
    survives LibreOffice, unlike PowerPoint's autofit hints.
    """
    if budget <= 0 or len(text) <= budget:
        return
    scale = max(0.55, budget / len(text))
    for node in shape.iter():
        raw = node.get("sz")
        if raw:
            # Never below the guard's legibility floor. At 10pt the renderer
            # could only ever produce a deck the guard was certain to reject,
            # which is what turned one overlong card into four failed builds.
            node.set("sz", str(max(MIN_BODY_PT, int(int(raw) * scale))))


# A table read at presentation distance, not on a laptop. The stencil's own
# sizes are ~9pt, which nobody in a room can read.
_TABLE_HEADER_PT = 1600
_TABLE_BODY_PT = 1400
# 0.15in top and bottom. Generous padding is the brand's own instruction, and
# it also lets a short table use the content area instead of sitting in the
# top third above a blank half-slide.
_ROW_PAD_EMU = 210000
# A comparison table carries a name AND a sentence per row, so eight rows only
# fit if the padding comes down with them.
_COMPARE_PAD_EMU = 44000
_COMPARE_BODY_PT = 1250
_COMPARE_SUB_PT = 1000
_BAND_FILL = "F2F1FD"  # a very light tint of the brand blue
# The label column carries a short noun; the description columns need the room.
_LABEL_COL_SHARE = 0.24
# A comparison table's feature column carries a name AND a sentence, so it
# needs far more of the width than a label column does.
_COMPARE_COL_SHARE = 0.34
_SUB_LINE_COLOUR = "5B5B66"  # the plain line under a bold feature name
_HIGHLIGHT_FILL = "E9E6FD"  # the subject column, a tint of the brand blue
_SUB_LINE_PT = 1100
# A comparison mark is read as a colour before it is read as a glyph, so each
# carries its own. Grey for absent rather than red: a competitor not having a
# feature is a fact, not a failure, and a wall of red reads as a sales sheet.
_MARK_COLOURS = {"\u2713": "2C12E7", "\u25d0": "FF7800", "\u2715": "9A9AA5"}
_MARK_PT = 1800


def _style_table(
    tbl: ET.Element, width_emu: int, *, highlight_col: int = 0, center_from: int = 0
) -> None:
    """Widen, pad, left-align and band the stencil's table.

    The stencil is drawn for five short columns of sample figures. Real content
    is two or three columns of sentences, so the geometry has to change with it:
    the label column narrows, the description columns take the rest, and rows
    gain vertical padding.
    """
    grid = tbl.find(_q("a:tblGrid"))
    cols = grid.findall(_q("a:gridCol")) if grid is not None else []
    comparing = center_from > 0
    if cols:
        share = _COMPARE_COL_SHARE if comparing else _LABEL_COL_SHARE
        label_w = int(width_emu * share) if len(cols) > 1 else width_emu
        rest = (width_emu - label_w) // max(1, len(cols) - 1) if len(cols) > 1 else 0
        for i, col in enumerate(cols):
            col.set("w", str(label_w if i == 0 else rest))

    for row_index, tr in enumerate(tbl.findall(_q("a:tr"))):
        header = row_index == 0
        for col_index, cell in enumerate(tr.findall(_q("a:tc")), start=1):
            props = cell.find(_q("a:tcPr"))
            if props is None:
                props = ET.SubElement(cell, _q("a:tcPr"))
            pad = _COMPARE_PAD_EMU if comparing else _ROW_PAD_EMU
            props.set("marT", str(pad))
            props.set("marB", str(pad))
            props.set("anchor", "ctr")
            # Banding helps the eye track across a wide row; the brand-blue
            # header the stencil already draws is left alone.
            if not header and row_index % 2 == 0:
                _set_cell_fill(props, _BAND_FILL)
            # The subject of the comparison is tinted the whole way down, so
            # the eye can find its column without reading the header again.
            if highlight_col and col_index == highlight_col and not header:
                _set_cell_fill(props, _HIGHLIGHT_FILL)
            centred = bool(center_from) and col_index >= center_from
            paras = list(cell.iter(_q("a:p")))
            for para_index, para in enumerate(paras):
                if centred:
                    _align_centre(para)
                else:
                    _align_left(para)
                mark = "".join(n.text or "" for n in para.iter(_q("a:t"))).strip()
                if not header and mark in _MARK_COLOURS:
                    _colour_runs(para, _MARK_COLOURS[mark])
                    _set_size(para, _MARK_PT)
                elif header:
                    _set_size(para, _TABLE_HEADER_PT)
                elif para_index and not centred:
                    # the plain sentence under a bold feature name
                    _set_size(para, _COMPARE_SUB_PT if comparing else _SUB_LINE_PT)
                else:
                    _set_size(para, _COMPARE_BODY_PT if comparing else _TABLE_BODY_PT)


def _set_cell_fill(props: ET.Element, color: str) -> None:
    for existing in props.findall(_q("a:solidFill")):
        props.remove(existing)
    fill = ET.Element(_q("a:solidFill"))
    ET.SubElement(fill, _q("a:srgbClr")).set("val", color)
    # a:solidFill must precede the border elements a stencil cell already has.
    props.insert(0, fill)


_SUBTITLE_PT = 1500


_SPARK_TOL_EMU = 137160  # 0.15in — tight enough to hit one shape, not a family


def _move_shape_near(root: ET.Element, spec: tuple[float, float, float, float]) -> None:
    """Relocate the decorative shape sitting at a known spot.

    Matched by position rather than by name because the template names every
    shape "object 4"; the tolerance is deliberately tight so this moves one
    diamond and never a whole family of them.
    """
    near_x, near_y, to_x, to_y = spec
    want = (int(near_x * EMU_PER_INCH), int(near_y * EMU_PER_INCH))
    for shape in _shapes(root):
        off = shape.find(f"./{_q('p:spPr')}/{_q('a:xfrm')}/{_q('a:off')}")
        if off is None:
            continue
        here = (int(off.get("x", "0")), int(off.get("y", "0")))
        if all(abs(a - b) <= _SPARK_TOL_EMU for a, b in zip(here, want, strict=True)):
            off.set("x", str(int(to_x * EMU_PER_INCH)))
            off.set("y", str(int(to_y * EMU_PER_INCH)))
            return


def _colour_paragraphs_after_first(shape: ET.Element, colour: str) -> None:
    """Repaint a slot's descriptor, leaving its headline paragraph alone."""
    body = shape.find(_q("p:txBody"))
    if body is None:
        return
    for para in body.findall(_q("a:p"))[1:]:
        for props in para.iter():
            if props.tag not in (f"{{{A}}}rPr", f"{{{A}}}defRPr", f"{{{A}}}endParaRPr"):
                continue
            for fill in props.findall(f"{{{A}}}solidFill"):
                props.remove(fill)
            solid = ET.SubElement(props, f"{{{A}}}solidFill")
            ET.SubElement(solid, f"{{{A}}}srgbClr").set("val", colour)


def _set_box(shape: ET.Element, x_in: float, y_in: float, w_in: float, h_in: float) -> None:
    """Give a shape explicit geometry, creating the xfrm when it has none.

    A shape without an xfrm inherits its box, and an inherited box cannot be
    argued with — writing one is the only way to guarantee a figure and its
    descriptor both fit.
    """
    props = shape.find(_q("p:spPr"))
    if props is None:
        props = ET.SubElement(shape, _q("p:spPr"))
    xfrm = props.find(_q("a:xfrm"))
    if xfrm is None:
        xfrm = ET.Element(_q("a:xfrm"))
        props.insert(0, xfrm)
    off = xfrm.find(_q("a:off")) or ET.SubElement(xfrm, _q("a:off"))
    ext = xfrm.find(_q("a:ext")) or ET.SubElement(xfrm, _q("a:ext"))
    off.set("x", str(int(x_in * EMU_PER_INCH)))
    off.set("y", str(int(y_in * EMU_PER_INCH)))
    ext.set("cx", str(int(w_in * EMU_PER_INCH)))
    ext.set("cy", str(int(h_in * EMU_PER_INCH)))


def _restyle(shape: ET.Element, *, size: int, colour: str, bold: bool) -> None:
    """Force size, colour and weight on every run of a shape."""
    for props in shape.iter():
        if props.tag not in (f"{{{A}}}rPr", f"{{{A}}}defRPr", f"{{{A}}}endParaRPr"):
            continue
        props.set("sz", str(size))
        props.set("b", "1" if bold else "0")
        for fill in props.findall(f"{{{A}}}solidFill"):
            props.remove(fill)
        solid = ET.SubElement(props, f"{{{A}}}solidFill")
        ET.SubElement(solid, f"{{{A}}}srgbClr").set("val", colour)


def _figure_pt(figure: str, base: int) -> int:
    """A KPI figure's point size from its length. The column is 2.6in wide:
    at 80pt about four characters fit, so "125,194.71" wrapped onto three lines
    and pushed its descriptor off the slide. Shorter figures keep the full size;
    longer ones step down so the number stays on one line."""
    # Measured in the rendered PDF: "1.5K" and "47.3" hold at 80pt; "125.2K"
    # still broke at 64pt and holds at ~50pt.
    n = len(figure.strip())
    if n <= 4:
        return base
    if n == 5:
        return int(base * 0.78)
    if n == 6:
        return int(base * 0.62)
    if n <= 8:
        return int(base * 0.52)
    if n <= 10:
        return int(base * 0.44)
    return int(base * 0.38)


def _enlarge_first_paragraph(shape: ET.Element, size: int) -> None:
    """Blow the label up. On a KPI slide the figure is the content, so it should
    dominate the card rather than sit above it at heading size."""
    body = shape.find(_q("p:txBody"))
    if body is None:
        return
    first = body.find(_q("a:p"))
    if first is not None:
        _set_size(first, size)


def _force_left(shape: ET.Element) -> None:
    """Left-align a title for real.

    Setting `algn` on the paragraph is not enough: a stencil can centre from its
    list style or centre the body itself, and both outrank an unset paragraph.
    The brand's hierarchy is left-aligned, and mixing left and centre inside one
    deck reads as accidental.
    """
    for para in shape.iter(_q("a:p")):
        _align_left(para)
    for level in shape.iter(_q("a:lvl1pPr")):
        level.set("algn", "l")
    for body in shape.iter(_q("a:bodyPr")):
        body.set("anchorCtr", "0")


def _colour_runs(para: ET.Element, colour: str) -> None:
    """Paint every run in a paragraph one explicit colour."""
    for run in para.findall(_q("a:r")):
        props = run.find(_q("a:rPr"))
        if props is None:
            props = ET.SubElement(run, _q("a:rPr"))
        for existing in props.findall(_q("a:solidFill")):
            props.remove(existing)
        fill = ET.Element(_q("a:solidFill"))
        ET.SubElement(fill, _q("a:srgbClr")).set("val", colour)
        props.insert(0, fill)


def _align_centre(para: ET.Element) -> None:
    """Centre a paragraph — a column of marks reads as a column, not as prose."""
    props = para.find(_q("a:pPr"))
    if props is None:
        props = ET.Element(_q("a:pPr"))
        para.insert(0, props)
    props.set("algn", "ctr")


def _align_left(para: ET.Element) -> None:
    props = para.find(_q("a:pPr"))
    if props is None:
        props = ET.Element(_q("a:pPr"))
        para.insert(0, props)
    props.set("algn", "l")


def _set_size(para: ET.Element, size: int) -> None:
    for node in para.iter():
        if node.tag in (_q("a:rPr"), _q("a:defRPr"), _q("a:endParaRPr")):
            node.set("sz", str(size))


def _fill_shape_like(cell: ET.Element, text: str) -> None:
    """Write a table cell, optionally as a bold name over a plain sentence.

    A value containing a newline becomes two paragraphs: the first keeps the
    cell's drawn weight and is set bold, the second is cloned from it and set
    lighter and smaller. A comparison row needs both — the name is what the
    reader scans for, the sentence is what they read once they have stopped.
    """
    body = cell.find(_q("a:txBody"))
    if body is None:
        return
    paras = body.findall(_q("a:p"))
    if not paras:
        return
    head, _, tail = text.partition("\n")
    _set_paragraph(paras[0], head)
    for para in paras[1:]:
        body.remove(para)
    if not tail.strip():
        return
    for run in paras[0].findall(_q("a:r")):
        props = run.find(_q("a:rPr"))
        if props is None:
            props = ET.SubElement(run, _q("a:rPr"))
        props.set("b", "1")
    second = copy.deepcopy(paras[0])
    _set_paragraph(second, tail.strip())
    for run in second.findall(_q("a:r")):
        props = run.find(_q("a:rPr"))
        if props is None:
            continue
        props.set("b", "0")
        for existing in props.findall(_q("a:solidFill")):
            props.remove(existing)
        fill = ET.Element(_q("a:solidFill"))
        ET.SubElement(fill, _q("a:srgbClr")).set("val", _SUB_LINE_COLOUR)
        props.insert(0, fill)
    body.append(second)


# --- the filler -------------------------------------------------------------


def _load(template: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(template)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _build_slide(
    parts: dict[str, bytes],
    spec: Slide,
    stencil: Stencil,
    deck_name: str,
    year: str,
) -> tuple[bytes, str | None]:
    root = ET.fromstring(parts[f"ppt/slides/slide{stencil.source}.xml"].decode())
    shapes = _shapes(root)
    parents = _parents(root)
    used: set[int] = set()

    def use(index: int) -> ET.Element:
        used.add(index)
        return shapes[index]

    if stencil.header_sps:
        name_sp, year_sp = stencil.header_sps
        name = use(name_sp)
        _fill_shape(name, _clip_header(deck_name))
        # Some stencils size this box for the two words "Presentation name"; a
        # real deck title wraps to a second line inside the header rule.
        _widen(name, _HEADER_MIN_IN)
        _fill_shape(use(year_sp), year)

    title_shape: ET.Element | None = None
    if stencil.title_sp is not None and spec.title:
        shape = use(stencil.title_sp)
        title_shape = shape
        _fill_shape(shape, spec.title)
        if stencil.title_at != (0.0, 0.0):
            _move(shape, *stencil.title_at)
        if stencil.title_size:
            _resize(shape, *stencil.title_size)
        _shrink_to_fit(shape, spec.title, stencil.title_chars)
    elif stencil.title_from is not None and spec.title:
        src, idx = stencil.title_from
        donor = _shapes(ET.fromstring(parts[f"ppt/slides/slide{src}.xml"].decode()))[idx]
        clone = copy.deepcopy(donor)
        _fill_shape(clone, spec.title)
        _move(clone, *stencil.title_at)
        if stencil.title_size:
            _resize(clone, *stencil.title_size)
        _shrink_to_fit(clone, spec.title, stencil.title_chars)
        tree = root.find(f".//{_q('p:spTree')}")
        if tree is None:
            raise TemplateFillError(f"slide {stencil.source} has no shape tree")
        tree.append(clone)
        title_shape = clone

    if stencil.subtitle_sp is not None and spec.subtitle:
        _fill_shape(use(stencil.subtitle_sp), spec.subtitle)
    elif stencil.subtitle_at is not None and spec.subtitle and title_shape is not None:
        # Cloned from the title so it inherits the template's own type, then set
        # smaller and muted so it reads as support rather than a second heading.
        line = copy.deepcopy(title_shape)
        _fill_shape(line, spec.subtitle)
        _move(line, *stencil.subtitle_at)
        _resize(line, *stencil.subtitle_size)
        _restyle(line, size=_SUBTITLE_PT, colour=OMANTEL_TOKENS.muted, bold=False)
        tree = root.find(f".//{_q('p:spTree')}")
        if tree is not None:
            tree.append(line)

    # One size for the whole KPI row: the longest figure sets it, so four
    # numbers read as a set rather than one shrunken outlier beside three big.
    row_pt = (
        min(_figure_pt(slot.label, stencil.big_label_pt) for slot in spec.slots)
        if stencil.big_label_pt and spec.slots
        else 0
    )
    for position, (slot, index) in enumerate(zip(spec.slots, stencil.slot_sps, strict=False)):
        shape = use(index)
        _fill_shape(shape, *((slot.label, slot.body) if slot.body else (slot.label,)))
        if stencil.slot_boxes and position < len(stencil.slot_boxes):
            _set_box(shape, *stencil.slot_boxes[position])
        if row_pt:
            _enlarge_first_paragraph(shape, row_pt)
        if stencil.slot_body_colour:
            _colour_paragraphs_after_first(shape, stencil.slot_body_colour)
        # Scale rather than cut. Truncating to fit lost the end of every
        # sentence, which is worse than a smaller point size.
        _shrink_to_fit(shape, f"{slot.label} {slot.body}", stencil.slot_chars)

    # Any slot the deck did not use goes — an unfilled card would otherwise
    # publish the template's placeholder copy.
    for index in stencil.slot_sps[len(spec.slots) :]:
        _drop(shapes[index], parents)
    # …and its frame and connector go with it, or the chain ends in an empty
    # diamond. The generic icons go on every slide regardless.
    for decor in stencil.slot_decor[len(spec.slots) :]:
        for index in decor:
            if index < len(shapes):
                _drop(shapes[index], parents)
    for index in stencil.icon_sps:
        if index < len(shapes):
            _drop(shapes[index], parents)

    if spec.image is not None and stencil.image_box is not None:
        _place_image(root, spec.image, stencil.image_box)

    if stencil.move_spark:
        _move_shape_near(root, stencil.move_spark)

    normalise_brand_colours(root)

    chart_xml: str | None = None
    if stencil.chart and spec.chart is not None:
        chart_xml = _swap_table_for_chart(root, spec.chart)
    elif stencil.table:
        _fill_table(
            root,
            spec.rows,
            highlight_col=spec.table_highlight_col,
            center_from=spec.table_center_from,
        )

    return bytes(ET.tostring(root, encoding="UTF-8", xml_declaration=True)), chart_xml


def fill_template(
    template: bytes,
    slides: list[Slide],
    *,
    deck_name: str,
    year: str,
    stencils: dict[str, Stencil] | None = None,
) -> bytes:
    """Render `slides` by cloning the template's designed slides.

    The template's own example slides are dropped; only the emitted ones remain.
    Masters, layouts, theme and media are carried across untouched, which is
    what keeps the logo, the header rule and the diamonds exactly as drawn.
    """
    book = stencils or OMANTEL
    if not slides:
        raise TemplateFillError("a deck needs at least one slide")
    parts = _load(template)

    out: dict[str, bytes] = {k: v for k, v in parts.items() if not k.startswith("ppt/slides/")}
    charts: list[int] = []
    notes: list[tuple[int, str]] = []
    # Source, author and licence for every embedded image, so the deck can be
    # audited later without going back to the generator.
    credits: list[str] = []
    for number, spec in enumerate(slides, start=1):
        stencil = book.get(spec.kind)
        if stencil is None:
            raise TemplateFillError(f"template has no stencil for '{spec.kind}'")
        slide_xml, chart_xml = _build_slide(parts, spec, stencil, deck_name, year)
        # A cloned slide must not inherit the stencil's identity fields —
        # two slides PowerPoint cannot tell apart render blank.
        slide_out = refresh_identity(slide_xml.decode("utf-8"), number)
        rels = parts[f"ppt/slides/_rels/slide{stencil.source}.xml.rels"].decode()
        # The id and the reference to it are written TOGETHER, from one value,
        # so they cannot drift — and it conforms, which `rIdSilaChart` did not.
        wanted = (chart_xml is not None) + (
            spec.image is not None and stencil.image_box is not None
        )
        fresh = iter(allocate_rel_ids(rels, wanted))
        if chart_xml is not None:
            charts.append(number)
            out[f"ppt/charts/chart{number}.xml"] = chart_xml.encode()
            rid = next(fresh)
            rels = rels.replace(
                "</Relationships>",
                f'<Relationship Id="{rid}" Type="{_CHART_REL_TYPE}"'
                f' Target="../charts/chart{number}.xml"/></Relationships>',
            )
            slide_out = slide_out.replace(_CHART_RID, rid)
        if spec.image is not None and stencil.image_box is not None:
            asset = spec.image
            data = getattr(asset, "data", b"")
            extension = getattr(asset, "extension", "jpg")
            out[f"ppt/media/silaImage{number}.{extension}"] = data
            rid = next(fresh)
            rels = rels.replace(
                "</Relationships>",
                f'<Relationship Id="{rid}" Type="{_IMAGE_REL_TYPE}"'
                f' Target="../media/silaImage{number}.{extension}"/></Relationships>',
            )
            slide_out = slide_out.replace(_IMAGE_RID, rid)
            credits.append(getattr(asset, "credit", lambda: "")())
        out[f"ppt/slides/slide{number}.xml"] = slide_out.encode("utf-8")
        out[f"ppt/slides/_rels/slide{number}.xml.rels"] = rels.encode()
        if spec.notes.strip():
            notes.append((number, spec.notes.strip()))

    if credits:
        manifest = "Image credits — source, author, licence:\n" + "\n".join(credits)
        notes.append((1, manifest))
    merged_notes: dict[int, str] = {}
    for number, text in notes:
        merged_notes[number] = f"{merged_notes[number]}\n{text}" if number in merged_notes else text
    for number, text in merged_notes.items():
        _attach_notes(out, number, text)

    out["[Content_Types].xml"] = _content_types(parts, len(slides), charts, sorted(merged_notes))

    out["ppt/_rels/presentation.xml.rels"], rids = _presentation_rels(parts, len(slides))
    out["ppt/presentation.xml"] = _presentation(parts, rids)

    # Hygiene, then the regression checks, BEFORE anything is written: a deck
    # that makes PowerPoint offer to repair it, or that quietly lost a slide's
    # content, must not reach the reader.
    out = prune_orphans(out)
    validate_package(
        out,
        Expectation(
            slide_count=len(slides),
            texts=tuple(_expected_texts(slides)),
            body_texts=tuple(
                text
                for slide in slides
                for slot in slide.slots
                for text in (slot.label, slot.body)
                if text.strip()
            ),
        ),
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, blob in out.items():
            archive.writestr(name, blob)
    return buffer.getvalue()


def _expected_texts(slides: list[Slide]) -> list[str]:
    """Every string the caller handed over, so the guard can prove none was lost.

    Table cells and chart categories are excluded: they land in parts the text
    scan does not read (a chart is its own part), and their loss is caught by
    the chart and table tests directly.
    """
    texts: list[str] = []
    for slide in slides:
        texts.append(slide.title)
        for slot in slide.slots:
            texts.extend((slot.label, slot.body))
    return [t for t in texts if t and t.strip()]


_NOTES_CT = "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"
_NOTES_REL = f"{R}/notesSlide"
_SLIDE_BACK_REL = f"{R}/slide"


def _attach_notes(out: dict[str, bytes], number: int, text: str) -> None:
    """Speaker notes carry what would not fit the card.

    The alternative was dropping it, which is how a sentence ended mid-word on
    the slide and nowhere else. A minimal notesSlide is enough: PowerPoint shows
    it in the presenter view and in the notes pane.
    """
    body = "".join(
        f'<a:p><a:r><a:t xml:space="preserve">{_xml_escape(line)}</a:t></a:r></a:p>'
        for line in text.split("\n")
        if line.strip()
    )
    out[f"ppt/notesSlides/notesSlide{number}.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:notes xmlns:a="{A}" xmlns:p="{P}" xmlns:r="{R}"><p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        "<p:grpSpPr/>"
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Notes Placeholder"/>'
        '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        '<p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr/>'
        f"<p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody></p:sp>"
        "</p:spTree></p:cSld></p:notes>"
    ).encode()
    out[f"ppt/notesSlides/_rels/notesSlide{number}.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{_SLIDE_BACK_REL}" Target="../slides/slide{number}.xml"/>'
        "</Relationships>"
    ).encode()
    slide_rels = out[f"ppt/slides/_rels/slide{number}.xml.rels"].decode()
    notes_rid = allocate_rel_ids(slide_rels, 1)[0]
    out[f"ppt/slides/_rels/slide{number}.xml.rels"] = slide_rels.replace(
        "</Relationships>",
        f'<Relationship Id="{notes_rid}" Type="{_NOTES_REL}"'
        f' Target="../notesSlides/notesSlide{number}.xml"/></Relationships>',
    ).encode()


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _content_types(
    parts: dict[str, bytes], count: int, charts: list[int], notes: list[int]
) -> bytes:
    xml = parts["[Content_Types].xml"].decode()
    # Every family we re-emit below is stripped FIRST. The Omantel template
    # happens to declare no chart or notes overrides, so only the slide strip
    # is load-bearing today — but a workspace template that ships a chart would
    # otherwise end up with that part declared twice, which is what happened in
    # the OKR builder. A part carries exactly one declaration (ECMA-376
    # §10.1.2.2.2), and a duplicate is a semantic error an XML parse accepts.
    # `[^>]*`, NOT `[^/]*`: a ContentType value contains slashes
    # ("application/vnd.openxmlformats-…"), so the old `[^/]*` could never
    # reach the closing `/>` and the strip silently matched nothing. Every
    # deck built by this path has been declaring its slides twice.
    xml = re.sub(
        r'<Override PartName="/ppt/(?:slides/slide|charts/chart|notesSlides/notesSlide)'
        r'\d+\.xml"[^>]*/>',
        "",
        xml,
    )
    adds = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="{_SLIDE_CT}"/>'
        for i in range(1, count + 1)
    )
    adds += "".join(
        f'<Override PartName="/ppt/charts/chart{i}.xml" ContentType="{_CHART_CT}"/>' for i in charts
    )
    adds += "".join(
        f'<Override PartName="/ppt/notesSlides/notesSlide{i}.xml" ContentType="{_NOTES_CT}"/>'
        for i in notes
    )
    return xml.replace("</Types>", f"{adds}</Types>").encode()


def _presentation_rels(parts: dict[str, bytes], count: int) -> tuple[bytes, list[str]]:
    xml = parts["ppt/_rels/presentation.xml.rels"].decode()
    xml = re.sub(r'<Relationship[^>]*Target="slides/slide\d+\.xml"[^>]*/>', "", xml)
    # Read EVERY id, not just well-formed ones: a pattern that matches only
    # `rId<n>` makes a malformed id invisible to the allocator, which can then
    # hand out one that collides with it. (`max` also needs a default — with no
    # numeric id left this raised ValueError.)
    declared = set(re.findall(r'Id="([^"]+)"', xml))
    next_id = max(
        (int(n) for i in declared if (n := i[3:]).isdigit() and i[:3] == "rId"), default=0
    )
    rids: list[str] = []
    while len(rids) < count:
        next_id += 1
        candidate = f"rId{next_id}"
        if candidate not in declared:
            rids.append(candidate)
    adds = "".join(
        f'<Relationship Id="{rid}" Type="{_SLIDE_REL}" Target="slides/slide{i}.xml"/>'
        for i, rid in enumerate(rids, start=1)
    )
    return xml.replace("</Relationships>", f"{adds}</Relationships>").encode(), rids


def _presentation(parts: dict[str, bytes], rids: list[str]) -> bytes:
    xml = parts["ppt/presentation.xml"].decode()
    entries = "".join(f'<p:sldId id="{256 + i}" r:id="{rid}"/>' for i, rid in enumerate(rids))
    return re.sub(
        r"<p:sldIdLst>.*?</p:sldIdLst>", f"<p:sldIdLst>{entries}</p:sldIdLst>", xml, flags=re.S
    ).encode()


# --- recognising a designed template ---------------------------------------

# A typeface that only appears in a corporate template's own drawn slides is a
# far better signal than a file hash, which any re-export from PowerPoint
# changes. Extend with (marker, book) as more templates are stencilled.
_DESIGNED: tuple[tuple[str, dict[str, Stencil]], ...] = (("FS Alvar", OMANTEL),)


def stencil_book_for(template: bytes) -> dict[str, Stencil] | None:
    """The stencil book for a template whose branding is DRAWN on its slides.

    Returns None for an ordinary template — one whose master and theme carry the
    brand — so the caller keeps using palette extraction for those. The book is
    only returned if the file still has every slide and shape the stencils
    index, so a re-authored template degrades to the palette path instead of
    filling text into whatever shape now sits at that position.
    """
    try:
        parts = _load(template)
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    for marker, book in _DESIGNED:
        if not any(
            marker in blob.decode("utf-8", "ignore")
            for name, blob in parts.items()
            if name.startswith("ppt/slides/slide")
        ):
            continue
        if _book_fits(parts, book):
            return book
    return None


def _book_fits(parts: dict[str, bytes], book: dict[str, Stencil]) -> bool:
    counts: dict[int, int] = {}
    for stencil in book.values():
        for source in {stencil.source, *(s for s, _ in [stencil.title_from or (0, 0)] if s)}:
            if source not in counts:
                blob = parts.get(f"ppt/slides/slide{source}.xml")
                if blob is None:
                    return False
                counts[source] = len(_shapes(ET.fromstring(blob.decode())))
        needed = [*stencil.slot_sps, *(stencil.header_sps or ())]
        if stencil.title_sp is not None:
            needed.append(stencil.title_sp)
        if needed and max(needed) >= counts[stencil.source]:
            return False
        if stencil.title_from and stencil.title_from[1] >= counts[stencil.title_from[0]]:
            return False
    return True
