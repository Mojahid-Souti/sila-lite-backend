"""Markdown → WordprocessingML, for the documents SILA writes.

`build_docx_sections` used to emit one raw paragraph per line, so a GFM table
arrived in Word as literal pipe characters ("| State | What it means |") and a
heading was indistinguishable from body text. The model writes Markdown because
that is what it writes everywhere else; the document builder simply never read
it.

Supported, because it is what actually turns up in these documents: headings,
pipe tables, bullet and numbered lists, blockquotes, bold/italic/code spans, and
the ```chart fence (rendered as a small table — a real chart is a deck or a
dashboard, and inventing one in raw OOXML would be worse than showing figures).

Complexity: O(n) over the source lines; each line is visited once.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

import structlog

log = structlog.get_logger(__name__)

# Brand ink, so a document reads as Sila rather than as Word's defaults.
INK = "171717"
MUTED = "6B7280"
RULE = "E5E7EB"
ACCENT = "2D13EA"  # the mark's blue

_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:-]*-{2,}[\s:|-]*\|?\s*$")


def _runs(text: str) -> str:
    """Inline spans → runs. Bold/italic/code become real formatting rather than
    surviving as asterisks and backticks in the finished document."""
    out: list[str] = []
    # Links keep their label; the URL follows in parentheses so a printed page
    # still carries the destination.
    text = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    pos = 0
    pattern = re.compile(r"\*\*(.+?)\*\*|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|`([^`]+)`")
    for m in pattern.finditer(text):
        if m.start() > pos:
            out.append(_run(text[pos : m.start()]))
        if m.group(1) is not None:
            out.append(_run(m.group(1), bold=True))
        elif m.group(2) is not None:
            out.append(_run(m.group(2), italic=True))
        else:
            out.append(_run(m.group(3) or "", mono=True))
        pos = m.end()
    if pos < len(text):
        out.append(_run(text[pos:]))
    return "".join(out) or _run("")


def _run(text: str, *, bold: bool = False, italic: bool = False, mono: bool = False) -> str:
    props = []
    if mono:
        props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _para(inner: str, *, spacing: int = 120, indent: int = 0, style: str = "") -> str:
    ind = f'<w:ind w:left="{indent}"/>' if indent else ""
    return f'<w:p><w:pPr>{style}<w:spacing w:after="{spacing}"/>{ind}</w:pPr>{inner}</w:p>'


def _heading(text: str, level: int) -> str:
    size = {1: 32, 2: 26, 3: 22, 4: 20}.get(level, 20)
    color = ACCENT if level <= 2 else INK
    run = (
        f'<w:r><w:rPr><w:b/><w:color w:val="{color}"/><w:sz w:val="{size}"/>'
        f'</w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'
    )
    return _para(run, spacing=160)


def _cell(text: str, *, header: bool = False, width: int = 3000) -> str:
    shade = '<w:shd w:val="clear" w:fill="F3F4F6"/>' if header else ""
    body = (
        f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'
        if header
        else _runs(text)
    )
    return (
        f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shade}</w:tcPr>'
        f'<w:p><w:pPr><w:spacing w:after="40"/></w:pPr>{body}</w:p></w:tc>'
    )


def _table(rows: list[list[str]]) -> str:
    """A real <w:tbl>. Column width is the page split evenly — Word reflows text
    inside a cell, so an even split beats guessing at content width."""
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    width = max(1200, 9360 // max(cols, 1))
    borders = (
        "<w:tblBorders>"
        + "".join(
            f'<w:{edge} w:val="single" w:sz="4" w:space="0" w:color="{RULE}"/>'
            for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        + "</w:tblBorders>"
    )
    head, *body = rows
    out = [
        f'<w:tbl><w:tblPr><w:tblW w:w="9360" w:type="dxa"/>{borders}</w:tblPr>',
        "<w:tr>"
        + "".join(_cell(c, header=True, width=width) for c in _pad(head, cols))
        + "</w:tr>",
    ]
    for row in body:
        out.append("<w:tr>" + "".join(_cell(c, width=width) for c in _pad(row, cols)) + "</w:tr>")
    out.append("</w:tbl>")
    # Word needs a paragraph after a table or the next block glues to it.
    out.append(_para(_run(""), spacing=80))
    return "".join(out)


def _pad(row: list[str], cols: int) -> list[str]:
    return [*row, *([""] * (cols - len(row)))]


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _chart_as_table(raw: str) -> str:
    """A ```chart fence, rendered as a small table.

    The chat UI draws these properly; a document gets the figures in a table
    rather than a chart invented in raw OOXML. Either way the numbers are the
    point, and they are no longer ASCII bars.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    rows = parsed if isinstance(parsed, list) else parsed.get("data")
    if not isinstance(rows, list) or not rows:
        return ""
    title = "" if isinstance(parsed, list) else str(parsed.get("title") or "")
    table = [["", "Count"]]
    table[0][0] = title or "Item"
    for r in rows[:40]:
        if isinstance(r, dict):
            table.append([str(r.get("label") or r.get("name") or ""), str(r.get("value") or "")])
    return _table(table) if len(table) > 1 else ""


def markdown_to_ooxml(text: str) -> list[str]:
    """Convert one Markdown body into a list of OOXML block strings."""
    blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Fenced block: a chart becomes a table; other code keeps its shape.
        if stripped.startswith("```"):
            lang = stripped[3:].strip().lower()
            i += 1
            body: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            payload = "\n".join(body)
            if lang == "chart":
                blocks.append(_chart_as_table(payload) or _para(_runs(payload)))
            else:
                for code_line in body:
                    blocks.append(_para(_run(code_line, mono=True), spacing=0))
            continue

        # A pipe table needs its separator row to be a table and not prose.
        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            rows = [_split_row(stripped)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            blocks.append(_table(rows))
            continue

        heading = _HEADING.match(stripped)
        if heading:
            blocks.append(_heading(heading.group(2), len(heading.group(1))))
            i += 1
            continue

        bullet = _BULLET.match(line)
        if bullet:
            blocks.append(_para(_run("•  ") + _runs(bullet.group(1)), spacing=60, indent=360))
            i += 1
            continue

        numbered = _NUMBERED.match(line)
        if numbered:
            blocks.append(_para(_runs(stripped), spacing=60, indent=360))
            i += 1
            continue

        if stripped.startswith(">"):
            quote = stripped.lstrip("> ").strip()
            blocks.append(
                _para(
                    f'<w:r><w:rPr><w:i/><w:color w:val="{MUTED}"/></w:rPr>'
                    f'<w:t xml:space="preserve">{escape(quote)}</w:t></w:r>',
                    indent=360,
                )
            )
            i += 1
            continue

        blocks.append(_para(_runs(stripped)))
        i += 1
    return blocks


def section_heading(text: str) -> str:
    """A section title, in the same voice as an in-body `##` heading."""
    return _heading(text, 2)


# The lockup ships as SVG and the API image has no SVG rasteriser, so it was
# rasterised once (Gotenberg's LibreOffice → PDF → pdftoppm → PIL crop, tinted to
# ACCENT because the source carries no fill and the web app colours it in CSS)
# and the PNG committed beside this module. Doing it per document would add a
# Gotenberg round-trip to every save for a logo that never changes.
_LOGO_PATH = Path(__file__).parent / "assets" / "sila-mark.png"
_LOGO_REL_ID = "rIdSilaMark"
# 0.24in tall at the lockup's 414:110 aspect. EMU: 914400 per inch.
_LOGO_H_EMU = 219456
_LOGO_W_EMU = 826038


@lru_cache(maxsize=1)
def brand_media() -> dict[str, bytes]:
    """The image parts a branded document needs, keyed by relationship id.

    Empty when the asset is missing, which is the whole fallback: `brand_header`
    then sets the wordmark as text and the document is still valid.
    """
    try:
        return {_LOGO_REL_ID: _LOGO_PATH.read_bytes()}
    except OSError:
        log.warning("docx.brand_logo_missing", path=str(_LOGO_PATH))
        return {}


def _logo_run() -> str:
    """The mark as an inline <w:drawing>, pointing at the relationship that
    `_docx_from_paragraphs` writes for `brand_media()`."""
    return (
        "<w:r><w:drawing>"
        f'<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{_LOGO_W_EMU}" cy="{_LOGO_H_EMU}"/>'
        '<wp:docPr id="1" name="Sila" descr="Sila"/>'
        "<a:graphic><a:graphicData "
        'uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="Sila"/><pic:cNvPicPr/>'
        f'</pic:nvPicPr><pic:blipFill><a:blip r:embed="{_LOGO_REL_ID}"/>'
        "<a:stretch><a:fillRect/></a:stretch></pic:blipFill>"
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/>'
        f'<a:ext cx="{_LOGO_W_EMU}" cy="{_LOGO_H_EMU}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
    )


def brand_header(title: str) -> list[str]:
    """A Sila title block: the mark, the document title, and a rule."""
    mark = (
        _logo_run()
        if brand_media()
        else (
            f'<w:r><w:rPr><w:b/><w:color w:val="{ACCENT}"/><w:sz w:val="24"/>'
            f'<w:spacing w:val="60"/></w:rPr><w:t>SILA</w:t></w:r>'
        )
    )
    rule = (
        "<w:p><w:pPr><w:pBdr>"
        f'<w:bottom w:val="single" w:sz="12" w:space="1" w:color="{ACCENT}"/>'
        '</w:pBdr><w:spacing w:after="240"/></w:pPr></w:p>'
    )
    return [
        _para(mark, spacing=40),
        _para(
            f'<w:r><w:rPr><w:b/><w:color w:val="{INK}"/><w:sz w:val="40"/></w:rPr>'
            f'<w:t xml:space="preserve">{escape(title)}</w:t></w:r>',
            spacing=60,
        ),
        rule,
    ]
