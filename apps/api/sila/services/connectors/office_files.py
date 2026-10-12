"""Minimal OOXML file generation — .docx and .pptx from plain text, stdlib only.

Graph has no content-authoring API for Word/PowerPoint (unlike Google Docs), so
"save my digest as a Word doc / a slide deck" means BUILDING the file and
uploading it to OneDrive (raw PUT). Both formats are ZIP containers of XML;
the skeletons below are the smallest sets Word/PowerPoint (incl. the web apps)
accept. No document library dependency — the stack is frozen.

All text is XML-escaped — model output can never inject markup.

Complexity: O(len(text)); one in-memory zip per file.
"""
# ruff: noqa: E501 — the OOXML template literals are structural; wrapping them
# would put whitespace inside XML attributes.

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
# Declared on the root because the brand mark is a <w:drawing>; Word rejects the
# part outright if a drawing's namespaces are missing.
_DRAWING_NS = (
    ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
    ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    ' xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
    ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)


def _title_paragraph(text: str, size: int) -> str:
    return (
        f'<w:p><w:pPr><w:rPr><w:b/><w:sz w:val="{size}"/></w:rPr></w:pPr>'
        f'<w:r><w:rPr><w:b/><w:sz w:val="{size}"/></w:rPr><w:t xml:space="preserve">'
        f"{escape(text)}</w:t></w:r></w:p>"
    )


def _docx_from_paragraphs(paragraphs: list[str], media: dict[str, bytes] | None = None) -> bytes:
    """Assemble the package. `media` maps a relationship id to PNG bytes, which is
    how the brand mark gets in: each entry becomes a part under word/media plus a
    relationship the document's <w:drawing> points at."""
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"{_DRAWING_NS}>'
        f"<w:body>{''.join(paragraphs)}</w:body></w:document>"
    )
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": _DOCX_CONTENT_TYPES,
        "_rels/.rels": _DOCX_RELS,
        "word/document.xml": document,
    }
    if media:
        rels = "".join(
            f'<Relationship Id="{rel_id}" Type="{_IMAGE_REL}" Target="media/{rel_id}.png"/>'
            for rel_id in media
        )
        parts["word/_rels/document.xml.rels"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{rels}</Relationships>"
        )
        for rel_id, blob in media.items():
            parts[f"word/media/{rel_id}.png"] = blob
    return _zip(parts)


def build_docx(title: str, text: str) -> bytes:
    """Plain text → a minimal valid .docx: a bold title paragraph + one paragraph
    per line. Word / Word Online open it and it can be edited normally."""
    paragraphs = [_title_paragraph(title, 32)]
    for line in text.splitlines() or [""]:
        paragraphs.append(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>')
    return _docx_from_paragraphs(paragraphs)


def build_docx_sections(title: str, sections: list[tuple[str, str]]) -> bytes:
    """Structured document: a Sila title block, then per section a heading and a
    Markdown-rendered body.

    Bodies go through `markdown_to_ooxml` because the model writes Markdown — it
    writes Markdown everywhere else. Emitting one raw paragraph per line put a
    GFM table into Word as literal pipes ("| State | What it means |") and made a
    heading indistinguishable from body text.

    O(total text length).
    """
    from sila.services.connectors.docx_markdown import (
        brand_header,
        brand_media,
        markdown_to_ooxml,
        section_heading,
    )

    paragraphs = list(brand_header(title))
    for heading, body in sections:
        if heading.strip():
            paragraphs.append(section_heading(heading.strip()))
        paragraphs.extend(markdown_to_ooxml(body))
    return _docx_from_paragraphs(paragraphs, media=brand_media())


_PPTX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
</Types>"""

_PPTX_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>"""

_PPTX_PRESENTATION = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>
<p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>"""

_PPTX_PRESENTATION_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
</Relationships>"""

_PPTX_MASTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr/>
</p:spTree></p:cSld>
<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
</p:sldMaster>"""

_PPTX_MASTER_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>"""

_PPTX_LAYOUT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank">
<p:cSld><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr/>
</p:spTree></p:cSld>
<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>"""

_PPTX_LAYOUT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>"""

# The smallest theme PowerPoint accepts: full clrScheme + fontScheme + a
# minimal-but-complete fmtScheme (3 fills / 3 lines / 3 effects / 3 bg fills).
_PPTX_THEME = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Sila">
<a:themeElements>
<a:clrScheme name="Sila"><a:dk1><a:srgbClr val="000000"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2><a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2><a:accent3><a:srgbClr val="A5A5A5"/></a:accent3><a:accent4><a:srgbClr val="FFC000"/></a:accent4><a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="70AD47"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme>
<a:fontScheme name="Sila"><a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>
<a:fmtScheme name="Sila">
<a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>
<a:lnStyleLst><a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>
<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>
<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>
</a:fmtScheme>
</a:themeElements>
</a:theme>"""

_PPTX_SLIDE_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>"""


def build_pptx(title: str, text: str) -> bytes:
    """Plain text → a minimal valid .pptx: one slide, a title box + a body box
    with one bullet paragraph per line. PowerPoint / the web app open it."""
    body_paragraphs = "".join(
        f"<a:p><a:r><a:t>{escape(line)}</a:t></a:r></a:p>" for line in text.splitlines() or [""]
    )
    slide = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        "<p:cSld><p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        "<p:grpSpPr/>"
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="838200" y="365760"/><a:ext cx="10515600" cy="1325880"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="3200" b="1"/>'
        f"<a:t>{escape(title)}</a:t></a:r></a:p></p:txBody></p:sp>"
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Body"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="838200" y="1825625"/><a:ext cx="10515600" cy="4351338"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        f"<p:txBody><a:bodyPr/><a:lstStyle/>{body_paragraphs}</p:txBody></p:sp>"
        "</p:spTree></p:cSld>"
        "</p:sld>"
    )
    return _zip(
        {
            "[Content_Types].xml": _PPTX_CONTENT_TYPES,
            "_rels/.rels": _PPTX_ROOT_RELS,
            "ppt/presentation.xml": _PPTX_PRESENTATION,
            "ppt/_rels/presentation.xml.rels": _PPTX_PRESENTATION_RELS,
            "ppt/slideMasters/slideMaster1.xml": _PPTX_MASTER,
            "ppt/slideMasters/_rels/slideMaster1.xml.rels": _PPTX_MASTER_RELS,
            "ppt/slideLayouts/slideLayout1.xml": _PPTX_LAYOUT,
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _PPTX_LAYOUT_RELS,
            "ppt/theme/theme1.xml": _PPTX_THEME,
            "ppt/slides/slide1.xml": slide,
            "ppt/slides/_rels/slide1.xml.rels": _PPTX_SLIDE_RELS,
        }
    )


def _zip(parts: dict[str, str] | dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()
