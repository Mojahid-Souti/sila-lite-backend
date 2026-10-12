"""Slides Engine v2 — low-level deterministic OOXML.

Positioned shapes (EMU coordinates), solid-fill rounded-rect cards, and
byte-stable packaging: fixed zip timestamps + fixed part order, so the
same Deck ALWAYS produces the identical .pptx bytes (pixel-stable by
construction — no autofit, no theme inheritance surprises).

Complexity: O(shapes) string assembly; no I/O beyond the in-memory zip.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from sila.services.slides.icons import IconDef

SLIDE_W = 12192000  # 16:9 in EMUs
SLIDE_H = 6858000


def _fill(color: str, alpha: int | None = None) -> str:
    """A solid fill, optionally translucent (alpha in thousandths: 6000 = 6%)."""
    if alpha is None:
        return f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
    return (
        f'<a:solidFill><a:srgbClr val="{color}"><a:alpha val="{alpha}"/></a:srgbClr></a:solidFill>'
    )


_XML_NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)
_ZIP_DATE = (2026, 1, 1, 0, 0, 0)  # fixed → byte-identical renders

_CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_TABLE_NS = "http://schemas.openxmlformats.org/drawingml/2006/table"
_CHART_CT = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
_CHART_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"


@dataclass(frozen=True)
class TableStyle:
    """Palette-derived table colors — render.py builds this from tokens so
    pptx.py stays palette-agnostic (it only knows shapes and colors). Text
    is kept on `row_fill`/`row_alt_fill` (never on the accent) so the WCAG
    gate's existing text-on-bg / text-on-surface guarantees cover it; the
    accent appears only as a bold rule under the header row."""

    header_text: str
    header_rule: str  # accent — a thick underline beneath the header
    row_fill: str
    row_alt_fill: str
    body_text: str
    stroke: str
    font: str


@dataclass(frozen=True)
class SlideRel:
    """One relationship a slide part needs: a chart part (its XML) or an image
    part (its PNG bytes). Rel ids are assigned in creation order (rId2+, since
    rId1 is always the slide layout), so charts and images never collide."""

    kind: str  # "chart" | "image"
    data: str | bytes


@dataclass(frozen=True)
class RenderedSlide:
    """One slide's XML plus its embedded relationships (chart + image parts),
    in the order their rel ids were assigned."""

    xml: str
    rels: tuple[SlideRel, ...] = ()


@dataclass(frozen=True)
class Run:
    """One styled text run. `size` is in hundredths of a point (2000 = 20pt)."""

    text: str
    size: int
    color: str
    font: str
    bold: bool = False
    italic: bool = False
    tracking: int = 0  # letter-spacing in 1/100 pt (labels look better spaced)

    def xml(self) -> str:
        flags = f' b="{int(self.bold)}" i="{int(self.italic)}"'
        spc = f' spc="{self.tracking}"' if self.tracking else ""
        return (
            f'<a:r><a:rPr lang="en-US" sz="{self.size}"{flags}{spc}>'
            f'<a:solidFill><a:srgbClr val="{self.color}"/></a:solidFill>'
            f'<a:latin typeface="{escape(self.font)}"/></a:rPr>'
            f"<a:t>{escape(self.text)}</a:t></a:r>"
        )


@dataclass(frozen=True)
class Para:
    runs: tuple[Run, ...]
    align: str = "l"  # l | ctr | r
    space_before: int = 0  # points

    def xml(self) -> str:
        before = (
            f'<a:spcBef><a:spcPts val="{self.space_before * 100}"/></a:spcBef>'
            if self.space_before
            else ""
        )
        props = (
            f'<a:pPr algn="{self.align}">{before}</a:pPr>' if (self.align != "l" or before) else ""
        )
        return f"<a:p>{props}{''.join(r.xml() for r in self.runs)}</a:p>"


class ShapeList:
    """Accumulates a slide's shapes with sequential ids (id 1 = the group)."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._next_id = 2
        self._rels: list[SlideRel] = []

    def _take_id(self) -> int:
        shape_id = self._next_id
        self._next_id += 1
        return shape_id

    def _add_rel(self, rel: SlideRel) -> str:
        """Register a chart/image relationship; returns its rel id (rId2+)."""
        self._rels.append(rel)
        return f"rId{len(self._rels) + 1}"  # rId1 is the layout

    @property
    def rels(self) -> tuple[SlideRel, ...]:
        return tuple(self._rels)

    def rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        fill: str,
        *,
        stroke: str | None = None,
        rounded: bool = False,
        alpha: int | None = None,
    ) -> None:
        geom = (
            '<a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val 6000"/></a:avLst>'
            "</a:prstGeom>"
            if rounded
            else '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        )
        line = (
            f'<a:ln w="12700"><a:solidFill><a:srgbClr val="{stroke}"/></a:solidFill></a:ln>'
            if stroke
            else "<a:ln><a:noFill/></a:ln>"
        )
        self._parts.append(
            f'<p:sp><p:nvSpPr><p:cNvPr id="{self._take_id()}" name="Rect"/>'
            "<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
            f"{geom}{_fill(fill, alpha)}{line}</p:spPr>"
            "<p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>"
        )

    def preset(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        prst: str,
        *,
        fill: str | None = None,
        stroke: str | None = None,
        stroke_w: int = 12700,
        alpha: int | None = None,
    ) -> None:
        """A PowerPoint preset-geometry shape (an icon glyph, an arrow…)."""
        self._geom_shape(
            x,
            y,
            w,
            h,
            f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>',
            fill,
            stroke,
            stroke_w,
            alpha,
        )

    def path(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        path_d: str,
        *,
        fill: str | None = None,
        stroke: str | None = None,
        stroke_w: int = 12700,
    ) -> None:
        """A custom-geometry shape from path commands in a 100×100 space.
        An open, stroke-only path MUST carry fill="none" or PowerPoint
        won't draw the stroke (it tries to auto-fill and renders nothing)."""
        path_attrs = 'w="100" h="100"' + (' fill="none"' if stroke and not fill else "")
        geom = (
            "<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/>"
            '<a:rect l="0" t="0" r="100" b="100"/>'
            f"<a:pathLst><a:path {path_attrs}>{path_d}</a:path></a:pathLst></a:custGeom>"
        )
        self._geom_shape(x, y, w, h, geom, fill, stroke, stroke_w)

    def _geom_shape(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        geom: str,
        fill: str | None,
        stroke: str | None,
        stroke_w: int,
        alpha: int | None = None,
    ) -> None:
        fill_xml = _fill(fill, alpha) if fill else "<a:noFill/>"
        # CT_LineProperties order is strict: the fill (solidFill) MUST precede
        # the join (round). Reversed, PowerPoint rejects the whole <a:ln> and
        # drops the stroke — which made the open stroked icons invisible.
        line = (
            f'<a:ln w="{stroke_w}" cap="rnd"><a:solidFill>'
            f'<a:srgbClr val="{stroke}"/></a:solidFill><a:round/></a:ln>'
            if stroke
            else "<a:ln><a:noFill/></a:ln>"
        )
        self._parts.append(
            f'<p:sp><p:nvSpPr><p:cNvPr id="{self._take_id()}" name="Shape"/>'
            "<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
            f"{geom}{fill_xml}{line}</p:spPr>"
            "<p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>"
        )

    def icon(self, icon_def: IconDef, x: int, y: int, size: int, color: str) -> None:
        """Draw a monochrome icon (from icons.py) at (x, y) in a size×size box."""
        for part in icon_def.parts:
            rx, ry, rw, rh = part.bbox
            px, py, pw, ph = int(x + rx * size), int(y + ry * size), int(rw * size), int(rh * size)
            fill = color if part.mode == "fill" else None
            stroke = color if part.mode == "stroke" else None
            if part.kind == "preset":
                self.preset(
                    px, py, pw, ph, part.spec, fill=fill, stroke=stroke, stroke_w=size // 12
                )
            else:
                self.path(px, py, pw, ph, part.spec, fill=fill, stroke=stroke, stroke_w=size // 10)

    def text(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        paragraphs: list[Para],
        *,
        anchor: str = "t",  # t | ctr | b
    ) -> None:
        shape_id = self._take_id()
        body = "".join(p.xml() for p in paragraphs)
        self._parts.append(
            f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="Text {shape_id}"/>'
            "<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
            f'<p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0"'
            f' anchor="{anchor}"/><a:lstStyle/>{body}</p:txBody></p:sp>'
        )

    def chart(self, x: int, y: int, w: int, h: int, chart_xml: str) -> None:
        """A native embedded chart (its part XML built by chartpart.py). The
        chart's rel id is rId2+ in the slide's rels; rId1 is the layout."""
        rel_id = self._add_rel(SlideRel("chart", chart_xml))
        frame_id = self._take_id()
        self._parts.append(
            f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{frame_id}" name="Chart {frame_id}"/>'  # noqa: E501
            "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
            f'<p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></p:xfrm>'
            f'<a:graphic><a:graphicData uri="{_CHART_NS}">'
            f'<c:chart xmlns:c="{_CHART_NS}" xmlns:r="'
            'http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
            f' r:id="{rel_id}"/></a:graphicData></a:graphic></p:graphicFrame>'
        )

    def picture(self, x: int, y: int, w: int, h: int, png_bytes: bytes) -> None:
        """An embedded raster picture (PNG). Fills the box; aspect is not locked
        so the caller's box governs the crop-free fit (stretch fill)."""
        rel_id = self._add_rel(SlideRel("image", png_bytes))
        pic_id = self._take_id()
        self._parts.append(
            f'<p:pic><p:nvPicPr><p:cNvPr id="{pic_id}" name="Picture {pic_id}"/>'
            '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
            f'<p:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch>'
            "</p:blipFill>"
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
        )

    def table(
        self, x: int, y: int, w: int, h: int, rows: list[list[str]], style: TableStyle
    ) -> None:
        """A native PowerPoint table (a:tbl in a graphicFrame) — editable,
        first row styled as a header, alternating body row fills."""
        if not rows:
            return
        cols = max(len(r) for r in rows)
        col_w = w // cols
        widths = [col_w] * (cols - 1) + [w - col_w * (cols - 1)]
        row_h = h // len(rows)
        grid = "".join(f'<a:gridCol w="{cw}"/>' for cw in widths)
        body = "".join(
            self._table_row(
                (r + [""] * cols)[:cols], row_h, style, header=(i == 0), band=(i % 2 == 0)
            )
            for i, r in enumerate(rows)
        )
        frame_id = self._take_id()
        self._parts.append(
            f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{frame_id}" name="Table {frame_id}"/>'  # noqa: E501
            "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
            f'<p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></p:xfrm>'
            f'<a:graphic><a:graphicData uri="{_TABLE_NS}"><a:tbl>'
            '<a:tblPr firstRow="1" bandRow="1"/>'
            f"<a:tblGrid>{grid}</a:tblGrid>{body}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
        )

    @staticmethod
    def _table_row(cells: list[str], h: int, style: TableStyle, *, header: bool, band: bool) -> str:
        fill = style.row_alt_fill if band else style.row_fill
        color = style.header_text if header else style.body_text
        rule = style.header_rule if header else style.stroke
        rule_w = 19050 if header else 6350
        cells_xml = "".join(
            (
                "<a:tc><a:txBody><a:bodyPr/><a:lstStyle/>"
                '<a:p><a:pPr algn="l"/><a:r>'
                f'<a:rPr lang="en-US" sz="1100" b="{int(header)}">'
                f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
                f'<a:latin typeface="{escape(style.font)}"/></a:rPr>'
                f"<a:t>{escape(cell)}</a:t></a:r></a:p></a:txBody>"
                '<a:tcPr marL="91440" marR="91440" marT="45720" marB="45720" anchor="ctr">'
                f'<a:lnB w="{rule_w}"><a:solidFill><a:srgbClr val="{rule}"/></a:solidFill></a:lnB>'
                f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill></a:tcPr></a:tc>'
            )
            for cell in cells
        )
        return f'<a:tr h="{h}">{cells_xml}</a:tr>'

    def xml(self) -> str:
        return "".join(self._parts)


def slide_part(shapes: ShapeList, bg: str) -> str:
    """A complete slideN.xml with an explicit solid background."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<p:sld {_XML_NS}>"
        f'<p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="{bg}"/></a:solidFill>'
        "<a:effectLst/></p:bgPr></p:bg><p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        f"<p:grpSpPr/>{shapes.xml()}</p:spTree></p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


def _content_types(slide_count: int, chart_count: int, has_images: bool) -> str:
    slide_overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, slide_count + 1)
    )
    chart_overrides = "".join(
        f'<Override PartName="/ppt/charts/chart{i}.xml" ContentType="{_CHART_CT}"/>'
        for i in range(1, chart_count + 1)
    )
    png_default = '<Default Extension="png" ContentType="image/png"/>' if has_images else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{png_default}"
        '<Override PartName="/ppt/presentation.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        f"{slide_overrides}{chart_overrides}"
        '<Override PartName="/ppt/theme/theme1.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.theme+xml"/>'
        "</Types>"
    )


_IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"


def _slide_rels(targets: list[tuple[str, str]]) -> str:
    """A slide's .rels: rId1 → its layout, then rId2+ for each (kind, target)
    where kind is 'chart' or 'image' — in the order the rel ids were assigned."""
    rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
    ]
    for offset, (kind, target) in enumerate(targets):
        rel_type = _CHART_REL if kind == "chart" else _IMAGE_REL
        rels.append(f'<Relationship Id="rId{offset + 2}" Type="{rel_type}" Target="{target}"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{''.join(rels)}</Relationships>"
    )


def _presentation(slide_count: int) -> str:
    slide_ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{2 + i}"/>' for i in range(slide_count))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<p:presentation {_XML_NS}>"
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst>"
        f'<p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}"/><p:notesSz cx="6858000" cy="9144000"/>'
        "</p:presentation>"
    )


def _presentation_rels(slide_count: int) -> str:
    slide_rels = "".join(
        f'<Relationship Id="rId{2 + i}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/slide" Target="slides/slide{i + 1}.xml"/>'
        for i in range(slide_count)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
        f"{slide_rels}</Relationships>"
    )


def package(slides: list[RenderedSlide]) -> bytes:
    """RenderedSlides → a complete .pptx, embedded chart + image parts included.
    Reuses the proven minimal master/layout/theme skeleton from office_files;
    fixed timestamps keep bytes stable. Image (PNG) parts are binary."""
    from sila.services.connectors import office_files as of

    total_charts = sum(sum(r.kind == "chart" for r in s.rels) for s in slides)
    has_images = any(r.kind == "image" for s in slides for r in s.rels)
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": _content_types(len(slides), total_charts, has_images),
        "_rels/.rels": of._PPTX_ROOT_RELS,
        "ppt/presentation.xml": _presentation(len(slides)),
        "ppt/_rels/presentation.xml.rels": _presentation_rels(len(slides)),
        "ppt/slideMasters/slideMaster1.xml": of._PPTX_MASTER,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": of._PPTX_MASTER_RELS,
        "ppt/slideLayouts/slideLayout1.xml": of._PPTX_LAYOUT,
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": of._PPTX_LAYOUT_RELS,
        "ppt/theme/theme1.xml": of._PPTX_THEME,
    }
    chart_no = image_no = 0
    for i, slide in enumerate(slides, start=1):
        parts[f"ppt/slides/slide{i}.xml"] = slide.xml
        targets: list[tuple[str, str]] = []
        for rel in slide.rels:
            if rel.kind == "chart":
                chart_no += 1
                parts[f"ppt/charts/chart{chart_no}.xml"] = rel.data
                targets.append(("chart", f"../charts/chart{chart_no}.xml"))
            else:
                image_no += 1
                parts[f"ppt/media/image{image_no}.png"] = rel.data
                targets.append(("image", f"../media/image{image_no}.png"))
        parts[f"ppt/slides/_rels/slide{i}.xml.rels"] = _slide_rels(targets)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()
