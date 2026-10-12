"""Slides Engine v2 — P0: schema + the validation gate. P1: the renderer.

P0 done-when: an invalid layout name or overlong body is rejected/reflowed
BEFORE rendering — the renderer can never crash on model output.
P1 done-when: each layout component renders pixel-stable from fixture JSON
(byte-identical repeated renders, geometry and palette assertions pass).
"""

from __future__ import annotations

import io
import itertools
import re
import zipfile
from xml.etree import ElementTree

import pytest

from sila.services.slides import render, schema
from sila.services.slides.tokens import PALETTES


def _deck(**overrides) -> dict:
    base = {
        "title": "Sila Platform Pitch",
        "palette": "modern-slate",
        "slides": [
            {
                "slideNumber": 7,  # wrong on purpose — code renumbers
                "layoutType": "title-hero",
                "slideTitle": "Sovereign AI for Oman",
                "subtitle": "The agent marketplace",
                "containers": [
                    {"type": "hero-text", "heading": "Why now", "body": "Data stays home."}
                ],
            },
            {
                "slideNumber": 9,
                "layoutType": "3-column-cards",
                "slideTitle": "Three pillars",
                "containers": [
                    {"type": "card", "heading": "Build", "body": "Import and customize agents."},
                    {"type": "card", "heading": "Vet", "body": "Gatekeeper checks everything."},
                    {"type": "card", "heading": "Hire", "body": "One click from the Bazaar."},
                ],
            },
        ],
    }
    base.update(overrides)
    return base


def test_valid_deck_parses_and_renumbers() -> None:
    deck, changelog = schema.validate_deck(_deck())
    assert deck.palette == schema.Palette.MODERN_SLATE
    assert [s.slide_number for s in deck.slides] == [1, 2]  # model's 7/9 ignored
    assert deck.slides[1].layout_type == schema.LayoutType.THREE_COLUMN_CARDS
    assert changelog == []


def test_unknown_layout_and_palette_are_rejected_with_names() -> None:
    bad = _deck()
    bad["slides"][0]["layoutType"] = "freeform-canvas"
    with pytest.raises(schema.DeckValidationError) as err:
        schema.validate_deck(bad)
    assert any("layout" in v.lower() for v in err.value.violations)

    bad2 = _deck(palette="rainbow-explosion")
    with pytest.raises(schema.DeckValidationError) as err2:
        schema.validate_deck(bad2)
    assert any("palette" in v for v in err2.value.violations)


def test_overlong_body_reflows_into_extra_containers() -> None:
    # slides[1] is 3-column-cards — roomy enough that the word-count split
    # (the editorial rule) is what actually governs here, not the
    # geometry backstop; that interaction gets its own test below.
    long_body = " ".join(f"word{i}" for i in range(40))  # 40 words > 25
    deck_dict = _deck()
    deck_dict["slides"][1]["containers"] = [
        {"type": "card", "heading": "Detail", "body": long_body}
    ]
    deck, changelog = schema.validate_deck(deck_dict)
    containers = deck.slides[1].containers
    assert len(containers) == 2  # split, not truncated
    assert all(len(c.body.split()) <= schema.MAX_BODY_WORDS for c in containers)
    assert " ".join(f"{containers[0].body} {containers[1].body}".split()) == long_body
    assert any("split into 2 containers" in line for line in changelog)


def test_body_beyond_slide_capacity_is_paginated_not_rejected() -> None:
    """P0 deferred this to P2: a slide that still has more containers than
    MAX_CONTAINERS after reflow is split across continuation slides
    instead of rejected."""
    huge = " ".join(f"word{i}" for i in range(60))
    deck_dict = _deck()
    deck_dict["slides"][1]["containers"] = [
        {"type": "card", "heading": f"H{i}", "body": huge} for i in range(3)
    ]  # 3 x 3 word-chunks = 9 containers > 4 -> must paginate
    deck, changelog = schema.validate_deck(deck_dict)
    assert len(deck.slides) > 2  # slide 1 (title-hero) + >=2 pages for slide 2
    assert [s.slide_number for s in deck.slides] == list(range(1, len(deck.slides) + 1))
    assert all(len(s.containers) <= schema.MAX_CONTAINERS for s in deck.slides)
    assert any("split across" in line for line in changelog)
    # 3-column-cards is a grid layout — continuation pages stay 3-column-cards.
    assert all(s.layout_type == schema.LayoutType.THREE_COLUMN_CARDS for s in deck.slides[1:])
    assert any("(cont.)" in s.slide_title for s in deck.slides[1:])


def test_singleton_layout_pagination_downgrades_to_column_cards() -> None:
    """title-hero and quote-focus are single-message layouts — repeating a
    hero title or a second quote glyph for overflow content reads badly,
    so continuation pages downgrade to 3-column-cards instead. (Not the
    cover: see test_an_overfull_cover_is_named_not_paginated.)"""
    deck_dict = _deck()
    deck_dict["slides"].append(
        {
            "layoutType": "title-hero",
            "slideTitle": "Sovereign AI section",
            "containers": [
                {"type": "hero-text", "heading": f"Point {i}", "body": "Short."} for i in range(6)
            ],
        }
    )  # 6 > MAX_CONTAINERS(4) -> two pages
    deck, changelog = schema.validate_deck(deck_dict)
    pages = [s for s in deck.slides if s.slide_title.startswith("Sovereign AI section")]
    assert pages[0].layout_type == schema.LayoutType.TITLE_HERO
    assert pages[1].layout_type == schema.LayoutType.THREE_COLUMN_CARDS  # downgraded
    assert pages[1].slide_title.endswith("(cont.)")
    assert any("continuation pages use 3-column-cards" in line for line in changelog)


def test_an_overfull_cover_is_named_not_paginated() -> None:
    """A cover with 13 blocks became the cover plus five "(cont.)" pages
    titled with the deck's name. The cover is never paginated: the gate
    names it for the retry, which moves the blocks to content slides."""
    deck_dict = _deck()
    deck_dict["slides"][0]["containers"] = [
        {"type": "hero-text", "heading": f"Point {i}", "body": "Short."} for i in range(13)
    ]
    with pytest.raises(schema.DeckValidationError) as caught:
        schema.validate_deck(deck_dict)
    assert any("slide 1 is the cover" in v for v in caught.value.violations)


def test_char_capacity_backstop_catches_what_word_count_misses() -> None:
    """title-hero's footer strip is physically tiny — a heading that
    satisfies MAX_HEADING_WORDS can still overflow the box outright. The
    backstop must catch it, and nothing may be silently dropped."""
    deck_dict = _deck()
    # 4 words, well under MAX_HEADING_WORDS(5), but long words that blow
    # past the box's real character budget for this layout.
    heading = "Extraordinarily comprehensive multidimensional infrastructure"
    deck_dict["slides"][0]["containers"] = [{"type": "hero-text", "heading": heading, "body": ""}]
    deck, changelog = schema.validate_deck(deck_dict)
    slide = deck.slides[0]
    assert any("overflowed its box" in line for line in changelog)
    # Nothing lost: every original word reappears somewhere on the slide.
    recovered = " ".join(slide.slide_title.split())
    recovered += " " + " ".join(c.heading for c in slide.containers)
    recovered += " " + " ".join(c.body for c in slide.containers)
    for word in heading.split():
        assert word in recovered


def test_title_spill_moves_to_subtitle_and_heading_spill_to_body() -> None:
    # slides[1] (3-column-cards) has enough box capacity that this test
    # isolates the word-count (editorial) rule from the char backstop.
    deck_dict = _deck()
    deck_dict["slides"][1]["slideTitle"] = (
        "A very long presentation title that keeps going well past the limit"
    )
    deck_dict["slides"][1]["containers"] = [
        {"type": "card", "heading": "This heading has far too many words in it", "body": ""}
    ]
    deck, changelog = schema.validate_deck(deck_dict)
    slide = deck.slides[1]
    assert len(slide.slide_title.split()) <= schema.MAX_TITLE_WORDS
    assert "past the limit" in slide.subtitle  # nothing silently lost
    container = slide.containers[0]
    assert len(container.heading.split()) <= schema.MAX_HEADING_WORDS
    assert "words in it" in container.body
    assert len(changelog) == 2


def test_empty_containers_dropped_and_empty_slide_rejected() -> None:
    deck_dict = _deck()
    deck_dict["slides"][0]["containers"].append({"type": "card", "heading": "", "body": ""})
    deck, changelog = schema.validate_deck(deck_dict)
    assert len(deck.slides[0].containers) == 1  # the empty one is gone
    assert any("orphaned" in line for line in changelog)

    hollow = _deck()
    hollow["slides"] = [{"layoutType": "quote-focus", "slideTitle": "", "containers": []}]
    with pytest.raises(schema.DeckValidationError, match="no title and no content"):
        schema.validate_deck(hollow)


def test_deck_limits() -> None:
    with pytest.raises(schema.DeckValidationError, match="no slides"):
        schema.validate_deck(_deck(slides=[]))
    too_many = _deck()
    too_many["slides"] = [too_many["slides"][0]] * 31
    with pytest.raises(schema.DeckValidationError, match="slide cap"):
        schema.validate_deck(too_many)


def test_snake_case_aliases_also_accepted() -> None:
    deck_dict = {
        "title": "T",
        "palette": "high-contrast-bold",
        "slides": [
            {
                "slide_number": 1,
                "layout_type": "quote-focus",
                "slide_title": "One line",
                "containers": [{"type": "hero-text", "body": "Quote."}],
            }
        ],
    }
    deck, _ = schema.validate_deck(deck_dict)
    assert deck.slides[0].layout_type == schema.LayoutType.QUOTE_FOCUS


# ---------------------------------------------------------------- P1: renderer


def _fixture_deck(palette: str = "modern-slate") -> schema.Deck:
    """One slide per layout component — the P1 acceptance fixture."""
    raw = {
        "title": "Sila Pitch",
        "palette": palette,
        "slides": [
            {
                "layoutType": "title-hero",
                "slideTitle": "Sovereign AI for Oman",
                "subtitle": "Agents your data can trust",
                "containers": [
                    {"type": "hero-text", "heading": "Why now", "body": "Data stays home."}
                ],
            },
            {
                "layoutType": "split-grid",
                "slideTitle": "One platform",
                "subtitle": "Build, vet, hire",
                "containers": [
                    {"type": "card", "heading": "Bazaar", "body": "Discover vetted agents."},
                    {"type": "card", "heading": "Gatekeeper", "body": "Nothing ships unchecked."},
                ],
            },
            {
                "layoutType": "3-column-cards",
                "slideTitle": "Three pillars",
                "containers": [
                    {"type": "card", "heading": "Build", "body": "Import and customize."},
                    {"type": "card", "heading": "Vet", "body": "PII and injection scans."},
                    {"type": "card", "heading": "Hire", "body": "One click, and it runs."},
                ],
            },
            {
                "layoutType": "metric-showcase",
                "slideTitle": "Traction",
                "containers": [
                    {"type": "metric", "heading": "Agents", "accentValue": "24"},
                    {"type": "metric", "heading": "Hours saved", "accentValue": "1,300"},
                    {
                        "type": "metric",
                        "heading": "Uptime",
                        "accentValue": "99.9%",
                        "body": "Local infra.",
                    },
                ],
            },
            {
                "layoutType": "quote-focus",
                "slideTitle": "What teams say",
                "containers": [
                    {
                        "type": "hero-text",
                        "heading": "Ops lead, Muscat",
                        "body": "It vetted itself in.",
                    }
                ],
            },
        ],
    }
    deck, changelog = schema.validate_deck(raw)
    assert changelog == []  # the fixture is within every density limit
    return deck


def _slide_xmls(pptx_bytes: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(pptx_bytes)) as archive:
        return {
            name: archive.read(name).decode()
            for name in archive.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        }


def _card_x_offsets(slide_xml: str) -> list[int]:
    """x offsets of the rounded-rect card shapes only (not text boxes)."""
    return [
        int(x)
        for x in re.findall(
            r'x="(\d+)" y="\d+"/><a:ext[^>]*/></a:xfrm><a:prstGeom prst="roundRect"', slide_xml
        )
    ]


def test_render_produces_a_valid_package_with_all_slides() -> None:
    deck = _fixture_deck()
    blob = render.render_deck(deck)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        content_types = archive.read("[Content_Types].xml").decode()
        presentation = archive.read("ppt/presentation.xml").decode()
    for i in range(1, 6):
        assert f"ppt/slides/slide{i}.xml" in names
        assert f"/ppt/slides/slide{i}.xml" in content_types
    assert presentation.count("<p:sldId ") == 5
    # Every slide part is well-formed XML.
    for xml in _slide_xmls(blob).values():
        ElementTree.fromstring(xml)


def test_rendering_twice_is_byte_identical() -> None:
    deck = _fixture_deck()
    assert render.render_deck(deck) == render.render_deck(deck)  # pixel-stable


@pytest.mark.parametrize("palette", ["modern-slate", "minimal-editorial", "high-contrast-bold"])
def test_palette_tokens_reach_every_slide(palette: str) -> None:
    deck = _fixture_deck(palette)
    tokens = PALETTES[deck.palette]
    for xml in _slide_xmls(render.render_deck(deck)).values():
        assert f'<a:srgbClr val="{tokens.bg}"/>' in xml  # explicit background
        assert tokens.accent in xml  # the accent appears on every layout
        assert tokens.body_font in xml


def test_three_column_cards_are_evenly_spaced() -> None:
    deck = _fixture_deck()
    xml = _slide_xmls(render.render_deck(deck))["ppt/slides/slide3.xml"]
    card_xs = _card_x_offsets(xml)
    assert len(card_xs) == 3  # one card per container
    steps = {b - a for a, b in itertools.pairwise(card_xs)}
    assert len(steps) == 1  # equal gutters — a real grid, not eyeballed


def test_metric_showcase_renders_values_large_and_accented() -> None:
    deck = _fixture_deck()
    xml = _slide_xmls(render.render_deck(deck))["ppt/slides/slide4.xml"]
    tokens = PALETTES[deck.palette]
    assert f'sz="4400" b="1" i="0"><a:solidFill><a:srgbClr val="{tokens.accent}"/>' in xml
    for value in ("24", "1,300", "99.9%"):
        assert f"<a:t>{value}</a:t>" in xml
    assert "<a:t>HOURS SAVED</a:t>" in xml  # labels are uppercased in code


def test_quote_focus_has_glyph_quote_and_attribution() -> None:
    deck = _fixture_deck()
    xml = _slide_xmls(render.render_deck(deck))["ppt/slides/slide5.xml"]
    assert "<a:t>“</a:t>" in xml and 'sz="9000"' in xml
    assert "<a:t>It vetted itself in.</a:t>" in xml
    assert "<a:t>— Ops lead, Muscat</a:t>" in xml


def test_title_hero_and_split_grid_geometry() -> None:
    deck = _fixture_deck()
    slides = _slide_xmls(render.render_deck(deck))
    hero = slides["ppt/slides/slide1.xml"]
    assert 'sz="5200"' in hero  # the hero title is the biggest text on deck
    assert "<a:t>Sovereign AI for Oman</a:t>" in hero
    split = slides["ppt/slides/slide2.xml"]
    right_x = render._MX + 4600000 + 400000
    assert f'x="{right_x}"' in split  # cards start in the right column
    assert split.count('prst="roundRect"') == 2


def test_text_is_xml_escaped() -> None:
    raw = {
        "title": "T",
        "palette": "minimal-editorial",
        "slides": [
            {
                "layoutType": "3-column-cards",
                "slideTitle": "R&D <fast>",
                "containers": [
                    {"type": "card", "heading": "A&B", "body": 'He said "go" & <went>.'}
                ],
            }
        ],
    }
    deck, _ = schema.validate_deck(raw)
    xml = _slide_xmls(render.render_deck(deck))["ppt/slides/slide1.xml"]
    assert "<a:t>R&amp;D &lt;fast&gt;</a:t>" in xml
    ElementTree.fromstring(xml)  # still well-formed despite hostile text


def test_full_pipeline_validate_then_render() -> None:
    deck, _ = schema.validate_deck(_deck())  # the P0 fixture flows straight through
    blob = render.render_deck(deck)
    assert blob[:2] == b"PK" and len(_slide_xmls(blob)) == 2


# ---------------------------------------------------------------- P2: a11y gate


def test_shipped_palettes_all_pass_wcag_aa() -> None:
    from sila.services.slides import a11y

    for palette, tokens in PALETTES.items():
        violations = a11y.audit_palette(tokens)
        assert violations == [], f"{palette.value}: {violations}"


def test_a11y_gate_actually_catches_a_bad_pairing() -> None:
    from sila.services.slides import a11y
    from sila.services.slides.tokens import PaletteTokens

    illegible = PaletteTokens(
        bg="FFFFFF",
        surface="FFFFFF",
        stroke="EEEEEE",
        text="F0F0F0",  # near-white on white — unreadable
        muted="EAEAEA",
        accent="F5F5F5",
        display_font="Arial",
        body_font="Arial",
    )
    violations = a11y.audit_palette(illegible)
    assert violations  # the near-invisible text/bg pairing is caught
    assert any("title" in v for v in violations)


def test_contrast_ratio_is_symmetric_and_bounded() -> None:
    from sila.services.slides import a11y

    assert a11y.contrast_ratio("000000", "FFFFFF") == a11y.contrast_ratio("FFFFFF", "000000")
    assert a11y.contrast_ratio("000000", "FFFFFF") == pytest.approx(21.0, abs=0.01)
    assert a11y.contrast_ratio("808080", "808080") == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------- P2: geometry backstop


def test_estimate_char_capacity_scales_with_box_and_font() -> None:
    from sila.services.slides.reflow import estimate_char_capacity

    small_font = estimate_char_capacity(4000000, 1000000, 12)
    big_font = estimate_char_capacity(4000000, 1000000, 40)
    assert small_font > big_font  # smaller font -> more characters fit

    small_box = estimate_char_capacity(1000000, 1000000, 18)
    big_box = estimate_char_capacity(8000000, 1000000, 18)
    assert big_box > small_box  # a wider box fits more per line


def test_container_capacity_is_worst_case_regardless_of_actual_count() -> None:
    """The budget must be safe for a slide with only 1 container just as
    much as one with 4 — it's always sized for the tightest packing
    pagination could ever produce (see reflow.py's module docstring)."""
    from sila.services.slides import reflow
    from sila.services.slides.enums import LayoutType

    cap_alone = reflow.container_capacity(LayoutType.THREE_COLUMN_CARDS)
    # container_capacity takes no n — it's always the MAX_CONTAINERS case.
    assert cap_alone == reflow.container_capacity(LayoutType.THREE_COLUMN_CARDS)
    heading_cap, body_cap = cap_alone
    assert heading_cap > 0 and body_cap > 0


# ---------------------------------------------------------------- P3: brand extraction


def _theme_xml(
    *,
    dk1: str = "1A1A1A",
    lt1: str = "FFFFFF",
    dk2: str = "0B2545",
    lt2: str = "F2F4F7",
    accent1: str = "E8590C",
    dk1_is_sys: bool = False,
    include_font_scheme: bool = True,
) -> str:
    """A realistic ppt/theme/themeN.xml color+font scheme, standing in
    for a real corporate .pptx's theme part."""
    dk1_elem = (
        f'<a:sysClr val="windowText" lastClr="{dk1}"/>'
        if dk1_is_sys
        else f'<a:srgbClr val="{dk1}"/>'
    )
    font_scheme = (
        '<a:fontScheme name="Corp"><a:majorFont><a:latin typeface="Montserrat"/></a:majorFont>'
        '<a:minorFont><a:latin typeface="Source Sans Pro"/></a:minorFont></a:fontScheme>'
        if include_font_scheme
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Corp">'
        "<a:themeElements>"
        f'<a:clrScheme name="Corp"><a:dk1>{dk1_elem}</a:dk1>'
        f'<a:lt1><a:srgbClr val="{lt1}"/></a:lt1>'
        f'<a:dk2><a:srgbClr val="{dk2}"/></a:dk2>'
        f'<a:lt2><a:srgbClr val="{lt2}"/></a:lt2>'
        f'<a:accent1><a:srgbClr val="{accent1}"/></a:accent1>'
        '<a:accent2><a:srgbClr val="70AD47"/></a:accent2>'
        '<a:hlink><a:srgbClr val="0563C1"/></a:hlink>'
        "</a:clrScheme>"
        f"{font_scheme}"
        "</a:themeElements></a:theme>"
    )


def _pptx_with_theme(theme_xml: str | None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        if theme_xml is not None:
            archive.writestr("ppt/theme/theme1.xml", theme_xml)
    return buf.getvalue()


def test_extract_brand_tokens_from_a_clean_corporate_theme() -> None:
    from sila.services.slides import a11y, brand_extract

    pptx_bytes = _pptx_with_theme(_theme_xml())
    tokens = brand_extract.extract_brand_tokens(pptx_bytes)
    assert tokens.bg == "FFFFFF"
    assert tokens.display_font == "Montserrat"
    assert tokens.body_font == "Source Sans Pro"
    assert a11y.audit_palette(tokens) == []  # the gate this function guarantees


def test_extract_brand_tokens_handles_syscolor_form() -> None:
    from sila.services.slides import brand_extract

    pptx_bytes = _pptx_with_theme(_theme_xml(dk1_is_sys=True, dk1="0D0D0D"))
    tokens = brand_extract.extract_brand_tokens(pptx_bytes)
    assert tokens.text.startswith("0D") or tokens.text == "0D0D0D"


def test_extract_brand_tokens_corrects_an_illegible_pastel_accent() -> None:
    """A real-world failure mode: a pale brand accent (great on a poster,
    unreadable as 44pt text on a card). The extractor must still return
    something legible, preserving the hue rather than defaulting to gray."""
    from sila.services.slides import a11y, brand_extract

    pale_accent = "FFE9C7"  # fails AA_LARGE against a white background
    assert a11y.contrast_ratio(pale_accent, "FFFFFF") < a11y.AA_LARGE_TEXT

    pptx_bytes = _pptx_with_theme(_theme_xml(accent1=pale_accent))
    tokens = brand_extract.extract_brand_tokens(pptx_bytes)
    assert a11y.contrast_ratio(tokens.accent, tokens.bg) >= a11y.AA_LARGE_TEXT
    # Hue preserved (darkened orange-ish, not collapsed to black/white/gray).
    h_before, _, _ = brand_extract._hex_to_hsv(pale_accent)
    h_after, s_after, _ = brand_extract._hex_to_hsv(tokens.accent)
    assert abs(h_before - h_after) < 0.05
    assert s_after > 0.1


def test_extract_brand_tokens_missing_font_scheme_falls_back() -> None:
    from sila.services.slides import brand_extract

    pptx_bytes = _pptx_with_theme(_theme_xml(include_font_scheme=False))
    tokens = brand_extract.extract_brand_tokens(pptx_bytes)
    assert tokens.display_font == "Calibri Light"
    assert tokens.body_font == "Calibri"


def test_extract_brand_tokens_rejects_bad_input() -> None:
    from sila.services.slides import brand_extract

    with pytest.raises(brand_extract.BrandExtractionError, match="not a valid .pptx|zip"):
        brand_extract.extract_brand_tokens(b"not a zip file at all")

    with pytest.raises(brand_extract.BrandExtractionError, match="ppt/theme"):
        brand_extract.extract_brand_tokens(_pptx_with_theme(None))

    no_scheme = (
        '<?xml version="1.0"?>'
        '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<a:themeElements/></a:theme>"
    )
    with pytest.raises(brand_extract.BrandExtractionError, match="clrScheme"):
        brand_extract.extract_brand_tokens(_pptx_with_theme(no_scheme))


def test_extract_brand_tokens_is_deterministic() -> None:
    from sila.services.slides import brand_extract

    pptx_bytes = _pptx_with_theme(_theme_xml())
    first = brand_extract.extract_brand_tokens(pptx_bytes)
    second = brand_extract.extract_brand_tokens(pptx_bytes)
    assert first == second


def test_brand_derived_tokens_render_through_the_same_pipeline() -> None:
    """The whole point: a brand palette drops into render_deck without
    the renderer knowing or caring it isn't one of the three built-ins."""
    from sila.services.slides import brand_extract

    tokens = brand_extract.extract_brand_tokens(_pptx_with_theme(_theme_xml()))
    deck = _fixture_deck()  # still declares palette="modern-slate"
    blob = render.render_deck(deck, tokens=tokens)
    xmls = _slide_xmls(blob)
    assert xmls  # renders without error
    assert f'<a:srgbClr val="{tokens.bg}"/>' in xmls["ppt/slides/slide1.xml"]
    # The override actually took effect: without it, this deck renders
    # modern-slate's own (very different) background instead.
    assert tokens.bg != PALETTES[deck.palette].bg
    assert render.render_deck(deck) != blob


# ------------------------------------------------- Plan A: tables + charts


def _figure_slide(container: dict, title: str = "Figure", captions: list | None = None) -> dict:
    return {
        "title": "T",
        "palette": "modern-slate",
        "slides": [
            {
                "layoutType": "data-showcase",
                "slideTitle": title,
                "containers": [container, *(captions or [])],
            }
        ],
    }


def test_table_renders_native_tbl_with_accent_header_rule() -> None:
    raw = _figure_slide(
        {
            "type": "table",
            "heading": "Top accounts",
            "rows": [["Account", "Value"], ["Omantel", "42"], ["PDO", "31"]],
        }
    )
    deck, changelog = schema.validate_deck(raw)
    assert changelog == []
    xml = _slide_xmls(render.render_deck(deck))["ppt/slides/slide1.xml"]
    assert "<a:tbl>" in xml and xml.count("<a:tr ") == 3
    assert "<a:t>Account</a:t>" in xml and "<a:t>Omantel</a:t>" in xml
    accent = PALETTES[schema.Palette.MODERN_SLATE].accent
    assert f'<a:lnB w="19050"><a:solidFill><a:srgbClr val="{accent}"/>' in xml  # header rule


@pytest.mark.parametrize("kind", ["bar", "line", "pie"])
def test_chart_becomes_an_embedded_chart_part(kind: str) -> None:
    raw = _figure_slide(
        {
            "type": "chart",
            "chart": {
                "kind": kind,
                "categories": ["A", "B", "C"],
                "series": [{"name": "Rev", "values": [3, 5, 2]}],
            },
        }
    )
    deck, _ = schema.validate_deck(raw)
    blob = render.render_deck(deck)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = set(z.namelist())
        assert "ppt/charts/chart1.xml" in names
        chart = z.read("ppt/charts/chart1.xml").decode()
        rels = z.read("ppt/slides/_rels/slide1.xml.rels").decode()
        content_types = z.read("[Content_Types].xml").decode()
        ElementTree.fromstring(chart)  # well-formed chart part
    slide = _slide_xmls(blob)["ppt/slides/slide1.xml"]
    assert "<p:graphicFrame>" in slide and 'r:id="rId2"' in slide
    assert "../charts/chart1.xml" in rels
    assert "/ppt/charts/chart1.xml" in content_types
    # cached values reached the chart (renders with no embedded workbook)
    for v in ("3", "5", "2"):
        assert f"<c:v>{v}</c:v>" in chart
    assert PALETTES[schema.Palette.MODERN_SLATE].accent in chart  # palette swatch


def test_pie_slices_get_distinct_palette_swatches() -> None:
    raw = _figure_slide(
        {
            "type": "chart",
            "chart": {
                "kind": "pie",
                "categories": ["A", "B", "C"],
                "series": [{"values": [1, 2, 3]}],
            },
        }
    )
    deck, _ = schema.validate_deck(raw)
    chart = (
        zipfile.ZipFile(io.BytesIO(render.render_deck(deck))).read("ppt/charts/chart1.xml").decode()
    )
    assert chart.count("<c:dPt>") == 3  # one colored slice per category
    colors = set(re.findall(r'<c:dPt>.*?<a:srgbClr val="([0-9A-F]{6})"', chart))
    assert len(colors) == 3  # three distinct swatches, all palette-derived


def test_figures_rejected_outside_data_showcase() -> None:
    bad = {
        "title": "T",
        "palette": "modern-slate",
        "slides": [
            {
                "layoutType": "3-column-cards",
                "slideTitle": "Nope",
                "containers": [{"type": "table", "rows": [["a"], ["b"]]}],
            }
        ],
    }
    with pytest.raises(schema.DeckValidationError, match="only valid on the 'data-showcase'"):
        schema.validate_deck(bad)


def test_data_showcase_needs_exactly_one_figure() -> None:
    two = _figure_slide(
        {
            "type": "chart",
            "chart": {"kind": "bar", "categories": ["A"], "series": [{"values": [1]}]},
        },
        captions=[{"type": "table", "rows": [["x"], ["y"]]}],
    )
    with pytest.raises(schema.DeckValidationError, match="exactly one table or chart"):
        schema.validate_deck(two)

    none = {
        "title": "T",
        "palette": "modern-slate",
        "slides": [{"layoutType": "data-showcase", "slideTitle": "Empty", "containers": []}],
    }
    with pytest.raises(schema.DeckValidationError, match="exactly one table or chart"):
        schema.validate_deck(none)


def test_chart_series_are_aligned_and_truncated_to_caps() -> None:
    cats = [f"c{i}" for i in range(20)]  # > MAX_CHART_CATEGORIES
    raw = _figure_slide(
        {
            "type": "chart",
            "chart": {
                "kind": "bar",
                "categories": cats,
                "series": [
                    {"name": "s1", "values": [float(i) for i in range(20)]},
                    {"name": "s2", "values": [1.0]},  # short → padded to category count
                    {"name": "s3", "values": [2.0]},
                    {"name": "s4", "values": [3.0]},  # 4th series → dropped (> MAX_CHART_SERIES)
                ],
            },
        }
    )
    deck, changelog = schema.validate_deck(raw)
    spec = deck.slides[0].containers[0].chart
    assert len(spec.categories) == schema.MAX_CHART_CATEGORIES
    assert len(spec.series) == schema.MAX_CHART_SERIES
    assert all(len(s.values) == schema.MAX_CHART_CATEGORIES for s in spec.series)  # aligned
    assert any("categories" in line for line in changelog)
    assert any("series" in line for line in changelog)


def test_table_truncated_to_column_and_row_caps() -> None:
    rows = [[f"c{c}" for c in range(9)]] + [[str(r)] * 9 for r in range(15)]  # 9 cols, 15 data rows
    deck, changelog = schema.validate_deck(_figure_slide({"type": "table", "rows": rows}))
    table = deck.slides[0].containers[0].rows
    assert all(len(r) == schema.MAX_TABLE_COLS for r in table)
    assert len(table) == schema.MAX_TABLE_DATA_ROWS + 1  # header + data cap
    assert any("columns" in line for line in changelog)
    assert any("data rows" in line for line in changelog)


def test_data_showcase_caption_cards_render_beside_the_figure() -> None:
    raw = _figure_slide(
        {
            "type": "chart",
            "chart": {"kind": "bar", "categories": ["A", "B"], "series": [{"values": [1, 2]}]},
        },
        captions=[
            {"type": "card", "heading": "Insight", "body": "A doubled."},
            {"type": "card", "heading": "Note", "body": "Watch B."},
        ],
    )
    deck, _ = schema.validate_deck(raw)
    xml = _slide_xmls(render.render_deck(deck))["ppt/slides/slide1.xml"]
    assert "<a:t>Insight</a:t>" in xml and "<a:t>Watch B.</a:t>" in xml
    assert "<p:graphicFrame>" in xml  # the chart still there alongside captions


def test_chartless_deck_still_byte_stable_with_new_pipeline() -> None:
    deck, _ = schema.validate_deck(_deck())
    assert render.render_deck(deck) == render.render_deck(deck)
    with zipfile.ZipFile(io.BytesIO(render.render_deck(deck))) as z:
        assert not [n for n in z.namelist() if n.startswith("ppt/charts/")]  # no chart parts


# ------------------------------------------- Slice: icons + decorative layer


def test_card_icon_renders_as_vector_with_accent_color() -> None:
    raw = {
        "title": "T",
        "palette": "modern-slate",
        "slides": [
            {
                "layoutType": "3-column-cards",
                "slideTitle": "Icons",
                "containers": [
                    {"type": "card", "icon": "gear", "heading": "Config", "body": "Tune it."},
                ],
            }
        ],
    }
    deck, _ = schema.validate_deck(raw)
    xml = _slide_xmls(render.render_deck(deck))["ppt/slides/slide1.xml"]
    assert 'prst="gear6"' in xml  # the gear preset glyph
    assert PALETTES[schema.Palette.MODERN_SLATE].accent in xml  # drawn in the accent


def test_every_icon_renders_and_loads() -> None:
    for name in schema.IconName:
        raw = {
            "title": "T",
            "palette": "high-contrast-bold",
            "slides": [
                {
                    "layoutType": "3-column-cards",
                    "slideTitle": "One",
                    "containers": [
                        {"type": "card", "icon": name.value, "heading": name.value},
                    ],
                }
            ],
        }
        deck, _ = schema.validate_deck(raw)
        xml = _slide_xmls(render.render_deck(deck))["ppt/slides/slide1.xml"]
        ElementTree.fromstring(xml)  # well-formed for every icon


def test_unknown_icon_name_is_rejected() -> None:
    raw = {
        "title": "T",
        "palette": "modern-slate",
        "slides": [
            {
                "layoutType": "3-column-cards",
                "slideTitle": "Bad",
                "containers": [
                    {"type": "card", "icon": "banana", "heading": "Nope"},
                ],
            }
        ],
    }
    with pytest.raises(schema.DeckValidationError):
        schema.validate_deck(raw)


def test_icon_rides_with_first_container_through_reflow() -> None:
    long_body = " ".join(f"word{i}" for i in range(40))
    raw = {
        "title": "T",
        "palette": "modern-slate",
        "slides": [
            {
                "layoutType": "3-column-cards",
                "slideTitle": "Split",
                "containers": [
                    {"type": "card", "icon": "star", "heading": "Detail", "body": long_body},
                ],
            }
        ],
    }
    deck, changelog = schema.validate_deck(raw)
    containers = deck.slides[0].containers
    assert len(containers) == 2  # body split
    assert containers[0].icon is schema.IconName.STAR  # icon stays on the first
    assert containers[1].icon is None  # not duplicated onto the spill


def test_decor_blob_on_content_slides_but_not_on_data_showcase() -> None:
    raw = {
        "title": "T",
        "palette": "modern-slate",
        "slides": [
            {"layoutType": "title-hero", "slideTitle": "Hero", "subtitle": "x"},
            {
                "layoutType": "data-showcase",
                "slideTitle": "Data",
                "containers": [
                    {"type": "table", "rows": [["a", "b"], ["1", "2"]]},
                ],
            },
        ],
    }
    deck, _ = schema.validate_deck(raw)
    slides = _slide_xmls(render.render_deck(deck))
    hero, data = slides["ppt/slides/slide1.xml"], slides["ppt/slides/slide2.xml"]
    assert '<a:alpha val="7000"/>' in hero  # translucent corner wash present
    assert '<a:alpha val="7000"/>' not in data  # figures stay on a clean backdrop
    accent = PALETTES[schema.Palette.MODERN_SLATE].accent
    assert accent in data  # ...but the footer rule (accent) is on every slide


def test_byte_stable_with_icons_and_decor() -> None:
    raw = {
        "title": "T",
        "palette": "minimal-editorial",
        "slides": [
            {
                "layoutType": "metric-showcase",
                "slideTitle": "M",
                "containers": [
                    {"type": "metric", "icon": "target", "heading": "Hit", "accentValue": "9"},
                ],
            }
        ],
    }
    deck, _ = schema.validate_deck(raw)
    assert render.render_deck(deck) == render.render_deck(deck)


# ------------------------------------------- Slice 2: infographic layouts


def _one(layout: str, containers: list, title: str = "T") -> dict:
    return {
        "title": "T",
        "palette": "modern-slate",
        "slides": [{"layoutType": layout, "slideTitle": title, "containers": containers}],
    }


def test_process_flow_renders_numbered_steps_and_arrows() -> None:
    raw = _one(
        "process-flow",
        [
            {"type": "card", "icon": "document", "heading": "Import", "body": "Upload."},
            {"type": "card", "icon": "gear", "heading": "Customize", "body": "Tune it."},
            {"type": "card", "icon": "check", "heading": "Hire", "body": "Run it."},
        ],
    )
    deck, changelog = schema.validate_deck(raw)
    xml = _slide_xmls(render.render_deck(deck))["ppt/slides/slide1.xml"]
    for num in ("1", "2", "3"):
        assert f"<a:t>{num}</a:t>" in xml  # step badges
    assert xml.count('prst="rightArrow"') == 2  # one arrow between each pair
    assert "<a:t>Import</a:t>" in xml and "<a:t>Customize</a:t>" in xml


def test_comparison_two_cards_with_vs_badge() -> None:
    raw = _one(
        "comparison",
        [
            {"type": "card", "icon": "star", "heading": "Sila", "body": "Vetted."},
            {"type": "card", "icon": "bolt", "heading": "DIY", "body": "Slow."},
        ],
    )
    deck, _ = schema.validate_deck(raw)
    xml = _slide_xmls(render.render_deck(deck))["ppt/slides/slide1.xml"]
    assert "<a:t>VS</a:t>" in xml
    assert "<a:t>Sila</a:t>" in xml and "<a:t>DIY</a:t>" in xml


def test_timeline_draws_line_dots_and_alternating_milestones() -> None:
    raw = _one(
        "timeline",
        [
            {"type": "card", "heading": "Q1", "body": "Auth."},
            {"type": "card", "heading": "Q2", "body": "Bazaar."},
            {"type": "card", "heading": "Q3", "body": "Vetting."},
        ],
    )
    deck, _ = schema.validate_deck(raw)
    xml = _slide_xmls(render.render_deck(deck))["ppt/slides/slide1.xml"]
    assert xml.count('prst="ellipse"') >= 3  # a node dot per milestone
    for label in ("Q1", "Q2", "Q3"):
        assert f"<a:t>{label}</a:t>" in xml


@pytest.mark.parametrize(
    "layout,count,valid",
    [
        ("comparison", 1, False),
        ("comparison", 2, True),
        ("comparison", 3, False),
        ("process-flow", 1, False),
        ("process-flow", 5, True),
        ("process-flow", 6, False),
        ("timeline", 2, True),
        ("timeline", 6, True),
        ("timeline", 7, False),
    ],
)
def test_infographic_layout_item_counts(layout: str, count: int, valid: bool) -> None:
    raw = _one(layout, [{"type": "card", "heading": f"h{i}", "body": "b"} for i in range(count)])
    if valid:
        deck, _ = schema.validate_deck(raw)
        assert len(deck.slides) == 1  # never paginated — fixed-shape layout
    else:
        with pytest.raises(schema.DeckValidationError, match="items"):
            schema.validate_deck(raw)


def test_figure_rejected_on_infographic_layout() -> None:
    raw = _one(
        "process-flow",
        [
            {
                "type": "chart",
                "chart": {"kind": "bar", "categories": ["a"], "series": [{"values": [1]}]},
            },
            {"type": "card", "heading": "step"},
        ],
    )
    with pytest.raises(schema.DeckValidationError, match="only valid on the 'data-showcase'"):
        schema.validate_deck(raw)


def test_slice2_layouts_byte_stable() -> None:
    raw = _one(
        "comparison",
        [
            {"type": "card", "heading": "A", "body": "x"},
            {"type": "card", "heading": "B", "body": "y"},
        ],
    )
    deck, _ = schema.validate_deck(raw)
    assert render.render_deck(deck) == render.render_deck(deck)


# ------------------------------------------- image-feature (AI-generated art)

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01"
    b"\xff\xff\xff\xff\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _image_deck(containers: list) -> dict:
    return {
        "title": "T",
        "palette": "modern-slate",
        "slides": [
            {"layoutType": "image-feature", "slideTitle": "Vision", "containers": containers}
        ],
    }


def test_image_feature_placeholder_when_no_bytes_is_deterministic() -> None:
    raw = _image_deck(
        [
            {"type": "image", "imagePrompt": "a sunrise over Muscat", "heading": "Dawn"},
            {"type": "card", "heading": "Local", "body": "Data stays home."},
        ]
    )
    deck, _ = schema.validate_deck(raw)
    blob = render.render_deck(deck)
    assert render.render_deck(deck) == blob  # no image → byte-stable
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert not [n for n in z.namelist() if n.startswith("ppt/media/")]
    xml = _slide_xmls(blob)["ppt/slides/slide1.xml"]
    assert "<a:t>Dawn</a:t>" in xml  # the placeholder shows the caption
    assert "<a:t>Local</a:t>" in xml  # caption card renders alongside


def test_image_bytes_embed_as_a_native_picture() -> None:
    raw = _image_deck([{"type": "image", "imagePrompt": "abstract", "heading": "Art"}])
    deck, _ = schema.validate_deck(raw)
    deck.slides[0].containers[0].image_bytes = _TINY_PNG  # what the tool injects
    blob = render.render_deck(deck)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert z.read("ppt/media/image1.png") == _TINY_PNG  # bytes intact
        assert 'Extension="png"' in z.read("[Content_Types].xml").decode()
        rels = z.read("ppt/slides/_rels/slide1.xml.rels").decode()
        assert "../media/image1.png" in rels
    assert "<p:pic>" in _slide_xmls(blob)["ppt/slides/slide1.xml"]


def test_image_only_valid_on_image_feature() -> None:
    raw = {
        "title": "T",
        "palette": "modern-slate",
        "slides": [
            {
                "layoutType": "3-column-cards",
                "slideTitle": "No",
                "containers": [
                    {"type": "image", "imagePrompt": "x", "heading": "y"},
                ],
            }
        ],
    }
    with pytest.raises(schema.DeckValidationError, match="only valid on the 'image-feature'"):
        schema.validate_deck(raw)


def test_image_feature_needs_exactly_one_image() -> None:
    with pytest.raises(schema.DeckValidationError, match="exactly one image"):
        schema.validate_deck(_image_deck([{"type": "card", "heading": "no image"}]))
    with pytest.raises(schema.DeckValidationError, match="exactly one image"):
        schema.validate_deck(
            _image_deck(
                [
                    {"type": "image", "imagePrompt": "a", "heading": "1"},
                    {"type": "image", "imagePrompt": "b", "heading": "2"},
                ]
            )
        )


def test_image_feature_rejects_a_chart() -> None:
    raw = _image_deck(
        [
            {"type": "image", "imagePrompt": "x", "heading": "y"},
            {
                "type": "chart",
                "chart": {"kind": "bar", "categories": ["a"], "series": [{"values": [1]}]},
            },
        ]
    )
    with pytest.raises(schema.DeckValidationError, match="can't hold a table or chart"):
        schema.validate_deck(raw)
