"""Template-filling engine: corporate branding preserved, our slides injected."""

from __future__ import annotations

import io
import re
import zipfile

import pytest

from sila.services.connectors.office_files import build_pptx
from sila.services.connectors.pptx_template import (
    Slide,
    TemplateError,
    fill_template,
    slides_from_outline,
)

_NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)


def _layout_xml(ltype: str, phs: str) -> bytes:
    return (
        f'<?xml version="1.0"?><p:sldLayout {_NS} type="{ltype}"><p:cSld><p:spTree>'
        f"{phs}</p:spTree></p:cSld></p:sldLayout>"
    ).encode()


def _fake_corporate_template() -> bytes:
    """Mirrors a REAL corporate deck (the live Omantel file): a logo media file,
    a type="title" layout (ctrTitle + subTitle) and a type="obj" layout whose
    content placeholder is UNTYPED (idx only). Placed at positions 2 and 3 —
    proving selection goes by TYPE, not file order (file order lied live)."""
    base = build_pptx("Corporate", "example content")
    buf = io.BytesIO(base)
    out = io.BytesIO()
    title_layout = _layout_xml(
        "title",
        '<p:sp><p:nvSpPr><p:nvPr><p:ph type="ctrTitle"/></p:nvPr></p:nvSpPr></p:sp>'
        '<p:sp><p:nvSpPr><p:nvPr><p:ph type="subTitle" idx="1"/></p:nvPr></p:nvSpPr></p:sp>'
        '<p:sp><p:nvSpPr><p:nvPr><p:ph type="ftr" idx="11"/></p:nvPr></p:nvSpPr></p:sp>',
    )
    obj_layout = _layout_xml(
        "obj",
        '<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr></p:sp>'
        '<p:sp><p:nvSpPr><p:nvPr><p:ph idx="1"/></p:nvPr></p:nvSpPr></p:sp>',
    )
    ct_extra = (
        b'<Default Extension="png" ContentType="image/png"/>'
        + b"".join(
            b'<Override PartName="/ppt/slideLayouts/slideLayout%d.xml" '
            b'ContentType="application/vnd.openxmlformats-officedocument.'
            b'presentationml.slideLayout+xml"/>' % i
            for i in (2, 3)
        )
        + b"</Types>"
    )
    with zipfile.ZipFile(buf) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "[Content_Types].xml":
                data = data.replace(b"</Types>", ct_extra)
            dst.writestr(info.filename, data)
        dst.writestr("ppt/media/logo.png", b"\x89PNG-fake-logo")
        rels = src.read("ppt/slideLayouts/_rels/slideLayout1.xml.rels")
        dst.writestr("ppt/slideLayouts/slideLayout2.xml", title_layout)
        dst.writestr("ppt/slideLayouts/_rels/slideLayout2.xml.rels", rels)
        dst.writestr("ppt/slideLayouts/slideLayout3.xml", obj_layout)
        dst.writestr("ppt/slideLayouts/_rels/slideLayout3.xml.rels", rels)
    return out.getvalue()


def test_fill_replaces_slides_and_keeps_branding() -> None:
    deck = fill_template(
        _fake_corporate_template(),
        "Q3 Review",
        [
            Slide("Q3 Review", ("Prepared by Sila",)),
            Slide("Revenue", ("Up 20%", "New segments")),
            Slide("Outlook", ("Hiring", "Expansion")),
        ],
    )
    z = zipfile.ZipFile(io.BytesIO(deck))
    names = set(z.namelist())
    # Branding assets survive verbatim.
    assert "ppt/media/logo.png" in names
    assert "ppt/slideMasters/slideMaster1.xml" in names
    assert "ppt/theme/theme1.xml" in names or any("theme" in n for n in names)
    # Exactly our 3 slides, wired end-to-end.
    slides = [n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    assert len(slides) == 3
    pres = z.read("ppt/presentation.xml").decode()
    assert pres.count("<p:sldId ") == 3
    rels = z.read("ppt/_rels/presentation.xml.rels").decode()
    assert rels.count('Target="slides/slide') == 3
    cts = z.read("[Content_Types].xml").decode()
    assert cts.count("/ppt/slides/slide") == 3
    # Content is placeholder-only (inherits the template's look) and escaped.
    # The placeholder attrs MIRROR the chosen layouts exactly: cover = the
    # type="title" layout's ctrTitle/subTitle; content = the obj layout's title
    # + UNTYPED idx="1" ph (a typed "body" would not bind — the live bug).
    s1 = z.read("ppt/slides/slide1.xml").decode()
    assert 'type="ctrTitle"' in s1 and 'type="subTitle"' in s1 and "Q3 Review" in s1
    s2 = z.read("ppt/slides/slide2.xml").decode()
    assert 'type="title"' in s2 and "Up 20%" in s2
    assert '<p:ph idx="1"/>' in s2  # untyped content ph, mirrored
    # Layouts chosen by TYPE (title→2, obj→3), not file order.
    r1 = z.read("ppt/slides/_rels/slide1.xml.rels").decode()
    r2 = z.read("ppt/slides/_rels/slide2.xml.rels").decode()
    assert "slideLayout2.xml" in r1 and "slideLayout3.xml" in r2


def test_xml_injection_is_escaped() -> None:
    deck = fill_template(
        _fake_corporate_template(),
        "x",
        [Slide('</a:t><evil x="1"/>', ("<script>alert(1)</script>",))],
    )
    s1 = zipfile.ZipFile(io.BytesIO(deck)).read("ppt/slides/slide1.xml").decode()
    assert "<evil" not in s1 and "<script>" not in s1
    assert "&lt;evil" in s1


def test_bad_files_raise_template_error() -> None:
    with pytest.raises(TemplateError):
        fill_template(b"not a zip at all", "t", [Slide("a")])
    fake_zip = io.BytesIO()
    with zipfile.ZipFile(fake_zip, "w") as zf:
        zf.writestr("hello.txt", "not powerpoint")
    with pytest.raises(TemplateError):
        fill_template(fake_zip.getvalue(), "t", [Slide("a")])


def test_outline_validation_caps_and_cleans() -> None:
    slides = slides_from_outline(
        [
            {"title": "One", "bullets": ["a", "", "b"]},
            {"title": "", "bullets": []},  # dropped: empty
            "junk",  # dropped: not a dict
            {"title": "Two", "bullets": "not-a-list"},
        ]
    )
    assert [s.title for s in slides] == ["One", "Two"]
    assert slides[0].bullets == ("a", "b")
    assert slides[1].bullets == ()
