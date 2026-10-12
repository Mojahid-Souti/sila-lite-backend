"""Built-in slide templates — a fixed starter gallery, zero setup.

Every workspace sees these alongside its own uploads (the Kimi pattern): pick
"Bold Blue" instead of starting from scratch. Each is a REAL generated .pptx
template — theme colors, a positioned master, a `type="title"` cover layout and
a `type="obj"` content layout — exactly the shapes our filler binds to, so the
output is styled by construction. Stdlib zipfile only (stack frozen).

Complexity: O(1) per template, generated once per process (cached).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

# 16:9 slide geometry (EMU). Title band on top, content band below; the cover
# layout centers its title. Placeholders carry positions here (master/layouts),
# so the filler's position-less slides inherit a designed look.
_W, _H = 12192000, 6858000


@dataclass(frozen=True)
class _Theme:
    slug: str
    name: str
    bg: str  # slide background
    ink: str  # body text
    title: str  # title text
    accent: str  # accent bar


THEMES: tuple[_Theme, ...] = (
    _Theme("sila-clean", "Sila Clean", "FFFFFF", "1F2937", "111827", "0EA5E9"),
    _Theme("bold-blue", "Bold Blue", "1D3FBF", "E7ECFF", "FFFFFF", "93C5FD"),
    _Theme("graphite", "Graphite", "16181D", "C9CDD6", "F3F4F6", "22D3EE"),
    _Theme("emerald", "Emerald", "F6FBF8", "1F3D2E", "0B3B25", "10B981"),
)


def _ph(ph_type: str | None, idx: str | None, x: int, y: int, w: int, h: int, sp_id: int) -> str:
    t_attr = f' type="{ph_type}"' if ph_type else ""
    i_attr = f' idx="{idx}"' if idx is not None else ""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{sp_id}" name="ph{sp_id}"/><p:cNvSpPr>'
        f'<a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph{t_attr}{i_attr}/></p:nvPr></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm></p:spPr>'
        f"<p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>"
    )


def _accent_bar(theme: _Theme, y: int) -> str:
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="9" name="bar"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="838200" y="{y}"/><a:ext cx="1371600" cy="76200"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{theme.accent}"/></a:solidFill></p:spPr>'
        "<p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>"
    )


_NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)


def _tree(theme: _Theme, shapes: str) -> str:
    return (
        f"<p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val='{theme.bg}'/></a:solidFill>"
        f"<a:effectLst/></p:bgPr></p:bg><p:spTree><p:nvGrpSpPr>"
        '<p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
        f"{shapes}</p:spTree></p:cSld>"
    )


def _master(theme: _Theme) -> str:
    shapes = _ph("title", None, 838200, 365125, 10515600, 1325563, 2) + _ph(
        "body", "1", 838200, 1990725, 10515600, 4351338, 3
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<p:sldMaster {_NS}>{_tree(theme, shapes)}"
        '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
        'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" '
        'hlink="hlink" folHlink="folHlink"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/>'
        '<p:sldLayoutId id="2147483650" r:id="rId2"/></p:sldLayoutIdLst>'
        "<p:txStyles>"
        f'<p:titleStyle><a:lvl1pPr><a:defRPr sz="3600" b="1"><a:solidFill>'
        f'<a:srgbClr val="{theme.title}"/></a:solidFill><a:latin typeface="Calibri"/>'
        "</a:defRPr></a:lvl1pPr></p:titleStyle>"
        f'<p:bodyStyle><a:lvl1pPr><a:spcBef><a:spcPts val="600"/></a:spcBef><a:buChar char="—"/>'
        f'<a:defRPr sz="1800"><a:solidFill><a:srgbClr val="{theme.ink}"/></a:solidFill>'
        '<a:latin typeface="Calibri"/></a:defRPr></a:lvl1pPr></p:bodyStyle>'
        "<p:otherStyle/></p:txStyles></p:sldMaster>"
    )


def _layout_title(theme: _Theme) -> str:
    shapes = (
        _accent_bar(theme, 3962400)
        + _ph("ctrTitle", None, 838200, 2286000, 10515600, 1500000, 2)
        + _ph("subTitle", "1", 838200, 4191000, 10515600, 900000, 3)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sldLayout {_NS} type="title" preserve="1">{_tree(theme, shapes)}'
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
    )


def _layout_content(theme: _Theme) -> str:
    shapes = (
        _accent_bar(theme, 1554480)
        + _ph("title", None, 838200, 365125, 10515600, 1100000, 2)
        + _ph(None, "1", 838200, 1990725, 10515600, 4351338, 3)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sldLayout {_NS} type="obj" preserve="1">{_tree(theme, shapes)}'
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
    )


def _theme_xml(theme: _Theme) -> str:
    c = {
        "dk1": theme.ink,
        "lt1": theme.bg,
        "dk2": theme.title,
        "lt2": theme.bg,
        "acc": theme.accent,
    }
    accents = "".join(
        f'<a:accent{i}><a:srgbClr val="{c["acc"]}"/></a:accent{i}>' for i in range(1, 7)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Sila">'
        '<a:themeElements><a:clrScheme name="Sila">'
        f'<a:dk1><a:srgbClr val="{c["dk1"]}"/></a:dk1><a:lt1><a:srgbClr val="{c["lt1"]}"/></a:lt1>'
        f'<a:dk2><a:srgbClr val="{c["dk2"]}"/></a:dk2><a:lt2><a:srgbClr val="{c["lt2"]}"/></a:lt2>'
        f"{accents}"
        '<a:hlink><a:srgbClr val="0563C1"/></a:hlink>'
        '<a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
        "</a:clrScheme>"
        '<a:fontScheme name="Sila"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/>'
        '<a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/>'
        '<a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
        '<a:fmtScheme name="Sila"><a:fillStyleLst>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
        '<a:lnStyleLst><a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
        '<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
        '<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
        "<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/>"
        "</a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>"
        '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>'
        "</a:fmtScheme></a:themeElements></a:theme>"
    )


_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT_DOC = "application/vnd.openxmlformats-officedocument"
_CT_LAYOUT = f"{_CT_DOC}.presentationml.slideLayout+xml"


def _rels(*rels: tuple[str, str, str]) -> str:
    """A .rels part from (rId, relationship kind, target) triples."""
    body = "".join(
        f'<Relationship Id="{rid}" Type="{_OD}/{kind}" Target="{target}"/>'
        for rid, kind, target in rels
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_RELS_NS}">{body}</Relationships>'
    )


@lru_cache(maxsize=8)
def builtin_template_bytes(slug: str) -> bytes | None:
    """The generated .pptx for a built-in theme (None for unknown slugs)."""
    theme = next((t for t in THEMES if t.slug == slug), None)
    if theme is None:
        return None
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/ppt/presentation.xml" '
            f'ContentType="{_CT_DOC}.presentationml.presentation.main+xml"/>'
            '<Override PartName="/ppt/slideMasters/slideMaster1.xml" '
            f'ContentType="{_CT_DOC}.presentationml.slideMaster+xml"/>'
            f'<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="{_CT_LAYOUT}"/>'
            f'<Override PartName="/ppt/slideLayouts/slideLayout2.xml" ContentType="{_CT_LAYOUT}"/>'
            f'<Override PartName="/ppt/theme/theme1.xml" ContentType="{_CT_DOC}.theme+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": _rels(("rId1", "officeDocument", "ppt/presentation.xml")),
        "ppt/presentation.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f"<p:presentation {_NS}>"
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            "<p:sldIdLst/>"
            f'<p:sldSz cx="{_W}" cy="{_H}"/><p:notesSz cx="{_H}" cy="{_W}"/>'
            "</p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": _rels(
            ("rId1", "slideMaster", "slideMasters/slideMaster1.xml"),
            ("rId2", "theme", "theme/theme1.xml"),
        ),
        "ppt/slideMasters/slideMaster1.xml": _master(theme),
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": _rels(
            ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", "slideLayout", "../slideLayouts/slideLayout2.xml"),
            ("rId3", "theme", "../theme/theme1.xml"),
        ),
        "ppt/slideLayouts/slideLayout1.xml": _layout_title(theme),
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _rels(
            ("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")
        ),
        "ppt/slideLayouts/slideLayout2.xml": _layout_content(theme),
        "ppt/slideLayouts/_rels/slideLayout2.xml.rels": _rels(
            ("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")
        ),
        "ppt/theme/theme1.xml": _theme_xml(theme),
    }
    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as z:
        for name, content in parts.items():
            z.writestr(name, content)
    return buf.getvalue()


def builtin_by_name(name: str) -> tuple[str, bytes] | None:
    """Resolve a built-in by display name (case-insensitive, fuzzy) → (name, bytes)."""
    n = name.strip().lower()
    for t in THEMES:
        if t.name.lower() == n or t.slug == n or n in t.name.lower():
            blob = builtin_template_bytes(t.slug)
            if blob:
                return (t.name, blob)
    return None
