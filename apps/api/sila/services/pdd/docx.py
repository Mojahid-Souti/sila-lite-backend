"""PDD Template — low-level OOXML builders, stdlib only (frozen stack).

A richer .docx than office_files' minimal one: real heading styles (with
outline levels so the ToC field works), shaded table headers, a Table of
Contents field Word refreshes on open (settings.xml updateFields), and a
classification footer with live page numbers. Every piece of text passes
through xml escape — content can never inject markup.

Complexity: O(content); one in-memory zip per document.
"""
# ruff: noqa: E501 — OOXML template literals are structural; wrapping them
# would put whitespace inside XML attributes.

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

_ACCENT = "1F2A44"  # header shading (deep navy) — brand-neutral, prints well
_LIGHT = "EDF0F7"  # zebra row shading

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
</Relationships>"""

# updateFields → Word refreshes the ToC (page numbers included) on open.
_SETTINGS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:updateFields w:val="true"/>
</w:settings>"""


def _style(
    style_id: str,
    name: str,
    *,
    size: int,
    bold: bool,
    color: str = "000000",
    outline: int | None = None,
    before: int = 0,
    after: int = 120,
) -> str:
    outline_xml = f'<w:outlineLvl w:val="{outline}"/>' if outline is not None else ""
    bold_xml = "<w:b/>" if bold else ""
    return (
        f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/>'
        f'<w:pPr><w:spacing w:before="{before}" w:after="{after}"/><w:keepNext/>{outline_xml}</w:pPr>'
        f'<w:rPr>{bold_xml}<w:color w:val="{color}"/><w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr></w:style>'
    )


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="21"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    + _style("Title", "Title", size=52, bold=True, color=_ACCENT, after=240)
    + _style("Subtitle", "Subtitle", size=26, bold=False, color="55607A", after=120)
    + _style("Heading1", "heading 1", size=32, bold=True, color=_ACCENT, outline=0, before=360)
    + _style("Heading2", "heading 2", size=26, bold=True, color=_ACCENT, outline=1, before=240)
    + _style("Heading3", "heading 3", size=22, bold=True, color="3A4664", outline=2, before=160)
    + "</w:styles>"
)

_FOOTER_TMPL = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:p><w:pPr><w:jc w:val="center"/><w:rPr><w:color w:val="8A93A8"/><w:sz w:val="16"/></w:rPr></w:pPr>'
    '<w:r><w:rPr><w:color w:val="8A93A8"/><w:sz w:val="16"/></w:rPr><w:t xml:space="preserve">{label} · Page </w:t></w:r>'
    '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    "</w:p></w:ftr>"
)


def run(
    text: str, *, bold: bool = False, italic: bool = False, color: str = "", size: int = 0
) -> str:
    props = "".join(
        [
            "<w:b/>" if bold else "",
            "<w:i/>" if italic else "",
            f'<w:color w:val="{color}"/>' if color else "",
            f'<w:sz w:val="{size}"/>' if size else "",
        ]
    )
    rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def paragraph(
    text: str,
    *,
    style: str = "",
    align: str = "",
    bold: bool = False,
    italic: bool = False,
    color: str = "",
    size: int = 0,
) -> str:
    props = "".join(
        [
            f'<w:pStyle w:val="{style}"/>' if style else "",
            f'<w:jc w:val="{align}"/>' if align else "",
        ]
    )
    ppr = f"<w:pPr>{props}</w:pPr>" if props else ""
    return f"<w:p>{ppr}{run(text, bold=bold, italic=italic, color=color, size=size)}</w:p>"


def bullet(text: str) -> str:
    return paragraph(f"•  {text}")


def page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def toc_field() -> str:
    """The ToC field (headings 1-3, hyperlinked, page numbers); Word populates
    it on open thanks to settings.xml updateFields."""
    return (
        "<w:p>"
        '<w:r><w:fldChar w:fldCharType="begin" w:dirty="true"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h \\z \\u </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        + run(
            "The table of contents populates when the document opens in Word.",
            italic=True,
            color="8A93A8",
        )
        + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "</w:p>"
    )


def _cell(text: str, *, header: bool = False, shade: str = "") -> str:
    fill = _ACCENT if header else shade
    shd = f'<w:shd w:val="clear" w:fill="{fill}"/>' if fill else ""
    para = paragraph(text, bold=header, color="FFFFFF" if header else "", size=18 if header else 0)
    return f'<w:tc><w:tcPr>{shd}<w:vAlign w:val="center"/></w:tcPr>{para}</w:tc>'


def table(headers: list[str], rows: list[list[str]]) -> str:
    """A bordered table: shaded bold header row + zebra body rows."""
    borders = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:color="C9CFDE"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    parts = [
        "<w:tbl><w:tblPr>"
        '<w:tblW w:w="5000" w:type="pct"/>'
        f"<w:tblBorders>{borders}</w:tblBorders>"
        '<w:tblCellMar><w:left w:w="108" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar>'
        "</w:tblPr>",
        "<w:tr>" + "".join(_cell(h, header=True) for h in headers) + "</w:tr>",
    ]
    for i, row in enumerate(rows):
        shade = _LIGHT if i % 2 else ""
        padded = [*row, *[""] * (len(headers) - len(row))][: len(headers)]
        parts.append("<w:tr>" + "".join(_cell(str(c), shade=shade) for c in padded) + "</w:tr>")
    parts.append("</w:tbl>")
    parts.append("<w:p/>")  # spacing after the table (also keeps tables apart)
    return "".join(parts)


def assemble(body_xml: str, *, footer_label: str) -> bytes:
    """Body XML → the complete .docx (styles, settings, footer, A4, margins)."""
    sect = (
        '<w:sectPr><w:footerReference w:type="default" r:id="rId2"/>'
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1200" w:bottom="1440" w:left="1200" w:header="720" w:footer="720"/>'
        "</w:sectPr>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{body_xml}{sect}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/settings.xml", _SETTINGS)
        z.writestr("word/footer1.xml", _FOOTER_TMPL.format(label=escape(footer_label)))
        z.writestr("word/document.xml", document)
    return buffer.getvalue()


def flow_diagram(steps: list[str], *, title: str = "") -> str:
    """A flowchart generated from structured steps — accent boxes joined by
    arrows, never a placeholder. Pure OOXML (a centered single-column table
    reads as a flow in Word and always prints)."""
    parts: list[str] = []
    if title:
        parts.append(paragraph(title, italic=True, color="55607A", size=18))
    for i, step in enumerate(steps):
        if i:
            parts.append(paragraph("▼", align="center", color="8A93A8"))
        box = (
            '<w:tbl><w:tblPr><w:jc w:val="center"/><w:tblW w:w="3600" w:type="pct"/>'
            "<w:tblBorders>"
            + "".join(
                f'<w:{side} w:val="single" w:sz="8" w:color="{_ACCENT}"/>'
                for side in ("top", "left", "bottom", "right")
            )
            + "</w:tblBorders></w:tblPr><w:tr><w:tc>"
            f'<w:tcPr><w:shd w:val="clear" w:fill="{_LIGHT}"/></w:tcPr>'
            + paragraph(f"{i + 1}. {step}", align="center", bold=True)
            + "</w:tc></w:tr></w:tbl>"
        )
        parts.append(box)
    parts.append("<w:p/>")
    return "".join(parts)


_RACI_FILL = {"A": _ACCENT, "R": "3E6DB5", "C": "8A93A8", "I": "D9DEEA"}


def raci_matrix(columns: list[str], rows: list[list[str]]) -> str:
    """The RACI matrix as a color-coded grid: A navy, R blue, C gray, I light —
    the rendered 'image' equivalent, generated from the derived table."""
    borders = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:color="C9CFDE"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    parts = [
        "<w:tbl><w:tblPr>"
        '<w:tblW w:w="5000" w:type="pct"/>'
        f"<w:tblBorders>{borders}</w:tblBorders></w:tblPr>",
        "<w:tr>" + "".join(_cell(c, header=True) for c in columns) + "</w:tr>",
    ]
    for row in rows:
        cells = [_cell(str(row[0]) if row else "")]
        for value in row[1:]:
            letter = str(value).strip().upper()
            fill = _RACI_FILL.get(letter, "")
            shd = f'<w:shd w:val="clear" w:fill="{fill}"/>' if fill else ""
            text_color = "FFFFFF" if letter in ("A", "R", "C") else ""
            cells.append(
                f"<w:tc><w:tcPr>{shd}</w:tcPr>"
                + paragraph(letter, align="center", bold=True, color=text_color)
                + "</w:tc>"
            )
        parts.append("<w:tr>" + "".join(cells) + "</w:tr>")
    parts.append("</w:tbl><w:p/>")
    return "".join(parts)
