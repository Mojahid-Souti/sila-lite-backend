"""Fill a corporate .pptx TEMPLATE with generated slides — stdlib only.

The Omantel pain point: staff have the branded template and the data but no
time to assemble the deck. The OOXML trick that makes this cheap: slides that
contain ONLY placeholders (`<p:ph type="title"/>`, `<p:ph type="body"/>`)
inherit every visual from the template's slide layouts/masters/theme — logos,
fonts, colors, footers all come free. So "fill the template" is ZIP surgery:

  1. keep the template archive verbatim (masters, layouts, theme, media),
  2. drop its example slides,
  3. inject placeholder-only slides wired to the template's OWN layouts,
  4. rewrite presentation.xml / rels / [Content_Types].xml accordingly.

All text is XML-escaped — model output can never inject markup. No pptx
library — the stack is frozen (same rule as office_files.py).

Complexity: O(zip entries + slides); one in-memory zip per deck.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

_MAX_SLIDES = 30
_MAX_BULLETS = 10
_TITLE_MAX = 200
_BULLET_MAX = 400

_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_SLIDE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
_REL_LAYOUT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
_CT_SLIDE = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"


@dataclass(frozen=True)
class Slide:
    title: str
    bullets: tuple[str, ...] = field(default_factory=tuple)


class TemplateError(ValueError):
    """The uploaded file isn't a usable .pptx template."""


def _sp_placeholder(ph_type: str | None, idx: str | None, paragraphs: list[str], sp_id: int) -> str:
    """One placeholder shape. No spPr/position — everything inherits from the
    template's layout. The type/idx attrs must MIRROR the layout's own
    placeholder EXACTLY (PowerPoint binds by type+idx; a mismatch falls back to
    plain master styling — the live "unformatted Omantel deck" bug)."""
    type_attr = f' type="{ph_type}"' if ph_type else ""
    idx_attr = f' idx="{idx}"' if idx is not None else ""
    paras = (
        "".join(f"<a:p><a:r><a:t>{escape(p[:_BULLET_MAX])}</a:t></a:r></a:p>" for p in paragraphs)
        or "<a:p/>"
    )
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{sp_id}" name="ph{sp_id}"/><p:cNvSpPr>'
        f'<a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph{type_attr}{idx_attr}/>'
        f"</p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>{paras}</p:txBody></p:sp>"
    )


@dataclass(frozen=True)
class _LayoutPick:
    """A chosen layout + the EXACT placeholder attrs to mirror on slides."""

    filename: str
    title_ph: tuple[str | None, str | None]  # (type, idx) as in the layout XML
    body_ph: tuple[str | None, str | None] | None


# Placeholder types that are chrome, never content targets.
_CHROME_PH = frozenset({"dt", "ftr", "sldNum", "pic", "media", "clipArt", "tbl", "chart", "dgm"})
# Content-layout type preference (PowerPoint's own naming): title+content first.
_CONTENT_TYPE_ORDER = ("obj", "tx", "txAndObj", "objAndTx", "secHead", "twoObj")


def _parse_layout(xml: str) -> tuple[str, list[tuple[str | None, str | None]]]:
    """(layout type attr, [(ph type, ph idx), …]) from a slideLayout XML."""
    m = re.search(r'<p:sldLayout[^>]*?\btype="([^"]+)"', xml)
    ltype = m.group(1) if m else ""
    phs: list[tuple[str | None, str | None]] = []
    for attrs in re.findall(r"<p:ph\b([^>/]*)/?>", xml):
        t = re.search(r'type="([^"]+)"', attrs)
        i = re.search(r'idx="([^"]+)"', attrs)
        phs.append((t.group(1) if t else None, i.group(1) if i else None))
    return ltype, phs


def _content_ph(phs: list[tuple[str | None, str | None]]) -> tuple[str | None, str | None] | None:
    """The layout's body/content placeholder: typed body/subTitle first, else the
    first UNTYPED ph (an 'obj' content placeholder — the Omantel case)."""
    for t, i in phs:
        if t in ("body", "subTitle"):
            return (t, i)
    for t, i in phs:
        if t is None:
            return (None, i)
    return None


def _title_ph(phs: list[tuple[str | None, str | None]]) -> tuple[str | None, str | None] | None:
    return next(((t, i) for t, i in phs if t in ("ctrTitle", "title")), None)


def _slide_xml(slide: Slide, pick: _LayoutPick) -> str:
    """A placeholder-only slide mirroring the chosen layout's placeholders."""
    t_type, t_idx = pick.title_ph
    shapes = [_sp_placeholder(t_type or "title", t_idx, [slide.title[:_TITLE_MAX]], 2)]
    if slide.bullets and pick.body_ph is not None:
        b_type, b_idx = pick.body_ph
        shapes.append(_sp_placeholder(b_type, b_idx, [b for b in slide.bullets[:_MAX_BULLETS]], 3))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sld xmlns:p="{_P}" xmlns:a="{_A}" xmlns:r="{_R}">'
        f'<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>'
        f"<p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>{''.join(shapes)}</p:spTree></p:cSld></p:sld>"
    )


def _pick_layouts(src: zipfile.ZipFile, names: list[str]) -> tuple[_LayoutPick, _LayoutPick]:
    """(cover, content) chosen by each layout's DECLARED type (file order lied on
    real corporate decks): cover = the type="title" layout; content = the first
    title+content layout by type preference, else any layout carrying both a
    title and a content placeholder."""
    ordered = sorted(names, key=lambda n: int(re.search(r"(\d+)", n).group(1)))  # type: ignore[union-attr]
    parsed: list[tuple[str, str, list[tuple[str | None, str | None]]]] = []
    for name in ordered:
        ltype, phs = _parse_layout(src.read(f"ppt/slideLayouts/{name}").decode("utf-8", "replace"))
        parsed.append((name, ltype, phs))

    def usable(phs: list[tuple[str | None, str | None]]) -> bool:
        return _title_ph(phs) is not None

    cover_row = next(
        (r for r in parsed if r[1] == "title" and usable(r[2])),
        next((r for r in parsed if usable(r[2])), parsed[0]),
    )
    content_row = None
    for wanted in _CONTENT_TYPE_ORDER:
        content_row = next(
            (r for r in parsed if r[1] == wanted and usable(r[2]) and _content_ph(r[2])), None
        )
        if content_row:
            break
    if content_row is None:
        content_row = next(
            (r for r in parsed if usable(r[2]) and _content_ph(r[2]) and r is not cover_row),
            cover_row,
        )

    def pick(row: tuple[str, str, list[tuple[str | None, str | None]]]) -> _LayoutPick:
        # Fallbacks keep content rendering even on placeholder-less layouts (the
        # minimal default base): generic title/body inherit master defaults.
        return _LayoutPick(
            filename=row[0],
            title_ph=_title_ph(row[2]) or ("title", None),
            body_ph=_content_ph(row[2]) or ("body", "1"),
        )

    return pick(cover_row), pick(content_row)


def fill_template(template_bytes: bytes, title: str, slides: list[Slide]) -> bytes:
    """Generate a deck FROM the template: its branding, our content.

    Raises TemplateError on a file that isn't a readable .pptx."""
    if not slides:
        slides = [Slide(title=title or "Presentation")]
    slides = slides[:_MAX_SLIDES]

    try:
        src = zipfile.ZipFile(io.BytesIO(template_bytes))
        entries = {i.filename for i in src.infolist()}
    except zipfile.BadZipFile as exc:
        raise TemplateError("That file isn't a .pptx (not a ZIP archive).") from exc
    if "ppt/presentation.xml" not in entries:
        raise TemplateError("That file isn't a PowerPoint template (missing presentation.xml).")

    layouts = [
        e.rsplit("/", 1)[1]
        for e in entries
        if e.startswith("ppt/slideLayouts/") and e.endswith(".xml") and "_rels" not in e
    ]
    if not layouts:
        raise TemplateError("The template has no slide layouts.")
    cover_pick, content_pick = _pick_layouts(src, layouts)

    old_slides = {
        e
        for e in entries
        if e.startswith("ppt/slides/") and (e.endswith(".xml") or e.endswith(".rels"))
    }

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            name = info.filename
            if name in old_slides:
                continue  # the template's example slides are dropped
            data = src.read(name)
            if name == "[Content_Types].xml":
                data = _rewrite_content_types(data.decode("utf-8"), len(slides)).encode("utf-8")
            elif name == "ppt/presentation.xml":
                data = _rewrite_presentation(data.decode("utf-8"), len(slides)).encode("utf-8")
            elif name == "ppt/_rels/presentation.xml.rels":
                data = _rewrite_presentation_rels(data.decode("utf-8"), len(slides)).encode("utf-8")
            out.writestr(name, data)

        for i, slide in enumerate(slides, start=1):
            pick = cover_pick if i == 1 else content_pick
            layout = pick.filename
            out.writestr(f"ppt/slides/slide{i}.xml", _slide_xml(slide, pick))
            out.writestr(
                f"ppt/slides/_rels/slide{i}.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{_REL_LAYOUT}" Target="../slideLayouts/{layout}"/>'
                "</Relationships>",
            )
    return out_buf.getvalue()


def _rewrite_content_types(xml: str, n_slides: int) -> str:
    """Drop the old slide Overrides; register ours."""
    xml = re.sub(r'<Override PartName="/ppt/slides/[^"]*"[^>]*?/>', "", xml)
    ours = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="{_CT_SLIDE}"/>'
        for i in range(1, n_slides + 1)
    )
    return xml.replace("</Types>", ours + "</Types>")


def _rewrite_presentation(xml: str, n_slides: int) -> str:
    """Replace the slide id list with ours (rIds sld1000+i — no collision with the
    template's master/theme rIds, which stay untouched)."""
    ids = "".join(f'<p:sldId id="{255 + i}" r:id="sld{1000 + i}"/>' for i in range(1, n_slides + 1))
    new_lst = f"<p:sldIdLst>{ids}</p:sldIdLst>"
    if re.search(r"<p:sldIdLst>.*?</p:sldIdLst>", xml, re.DOTALL):
        return re.sub(r"<p:sldIdLst>.*?</p:sldIdLst>", new_lst, xml, flags=re.DOTALL)
    if "<p:sldIdLst/>" in xml:
        return xml.replace("<p:sldIdLst/>", new_lst)
    # No slide list at all (rare) — insert after the master list.
    return re.sub(r"(</p:sldMasterIdLst>)", r"\1" + new_lst, xml, count=1)


def _rewrite_presentation_rels(xml: str, n_slides: int) -> str:
    """Drop old slide relationships; add ours (Ids sld1000+i, matching the
    presentation.xml r:ids)."""
    xml = re.sub(r'<Relationship [^>]*Type="[^"]*/slide"[^>]*/>', "", xml)
    ours = "".join(
        f'<Relationship Id="sld{1000 + i}" Type="{_REL_SLIDE}" Target="slides/slide{i}.xml"/>'
        for i in range(1, n_slides + 1)
    )
    return xml.replace("</Relationships>", ours + "</Relationships>")


def slides_from_outline(outline: list[dict[str, object]]) -> list[Slide]:
    """The MODEL's outline ([{title, bullets: [...]}, …]) → validated Slides.
    Strings only, capped everywhere — a hostile/confused outline can't break XML
    (escaped at render) or produce an unbounded deck."""
    out: list[Slide] = []
    for item in outline[:_MAX_SLIDES]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "") or "").strip()
        raw = item.get("bullets", [])
        bullets = tuple(
            str(b).strip()[:_BULLET_MAX]
            for b in (raw if isinstance(raw, list) else [])
            if str(b).strip()
        )[:_MAX_BULLETS]
        if title or bullets:
            out.append(Slide(title=title or "—", bullets=bullets))
    return out
