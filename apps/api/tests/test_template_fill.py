"""Clone-and-fill against the Omantel template.

These run against the real .pptx, because every defect this module exists to
fix was a property of that file rather than of the code: text split across
runs, spare table columns carrying the stencil's own headers, placeholder copy
left in unfilled cards.
"""

import io
import pathlib
import re
import zipfile

import pytest

from sila.services.slides.template_fill import (
    OMANTEL,
    OMANTEL_HIGHLIGHT,
    Slide,
    Slot,
    TemplateFillError,
    fill_template,
    stencil_book_for,
)
from sila.services.slides.template_map import deck_to_slides

TEMPLATE = pathlib.Path(__file__).parents[1] / "sila/services/slides/templates/omantel.pptx"
# The designed corporate template is not part of every edition.
pytestmark = pytest.mark.skipif(not TEMPLATE.is_file(), reason="corporate .pptx template absent")

OMANTEL_BLUE = "2D13EA"
OMANTEL_FONT = "FS Alvar"


@pytest.fixture(scope="module")
def template() -> bytes:
    return TEMPLATE.read_bytes()


def _slides(deck: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(deck)) as archive:
        names = sorted(
            (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.search(r"\d+", n.split("/")[-1]).group()),
        )
        return [archive.read(n).decode() for n in names]


def _text(xml: str) -> str:
    return " ".join(re.findall(r"<a:t[^>]*>([^<]*)</a:t>", xml))


def test_the_template_is_recognised_as_designed(template: bytes) -> None:
    assert stencil_book_for(template) is OMANTEL


def test_an_ordinary_file_is_not(template: bytes) -> None:
    """A non-pptx, or a template without the marker, must fall back to the
    palette path rather than have text written into arbitrary shapes."""
    assert stencil_book_for(b"not a pptx at all") is None


def test_emits_only_the_requested_slides(template: bytes) -> None:
    out = fill_template(
        template,
        [Slide("title", title="Q1 Review"), Slide("section", title="Adoption")],
        deck_name="Q1 Review",
        year="2026",
    )
    assert len(_slides(out)) == 2


def test_text_split_across_runs_is_still_replaced(template: bytes) -> None:
    """The header is three runs ('Presentation' / ' ' / 'name'); a naive replace
    of the joined string matches nothing and leaves the placeholder."""
    out = fill_template(
        template, [Slide("section", title="Adoption")], deck_name="Q1 Review", year="2026"
    )
    body = _text(_slides(out)[0])
    assert "Q1 Review" in body
    assert "Presentation" not in body


def test_placeholder_copy_never_survives(template: bytes) -> None:
    out = fill_template(
        template,
        [
            Slide("title", title="Q1 Review"),
            Slide("points", title="How it works", slots=(Slot("One", "First thing."),)),
            Slide("points4", title="Four", slots=tuple(Slot(f"L{i}", f"B{i}") for i in range(4))),
        ],
        deck_name="Q1 Review",
        year="2026",
    )
    joined = " ".join(_text(x) for x in _slides(out))
    for lorem in ("wonderful serenity", "Headline goes here", "Timeline", "Lorem"):
        assert lorem not in joined


def test_unfilled_slots_are_dropped_not_left_holding_the_template(template: bytes) -> None:
    """A three-card stencil given one card must publish ONE card — the spare is
    removed rather than filled with whatever content is left over."""
    out = fill_template(
        template,
        [Slide("points", title="One thing", slots=(Slot("Only", "The single point."),))],
        deck_name="Q1 Review",
        year="2026",
    )
    assert _text(_slides(out)[0]).count("The single point.") == 1


def test_table_is_trimmed_to_the_data_width(template: bytes) -> None:
    """The stencil table is 5 columns. Three columns of data must not publish
    the template's spare headers ('Income', 'Statement') as if they were real."""
    out = fill_template(
        template,
        [Slide("table", title="Hire or build", rows=(("A", "B", "C"), ("1", "2", "3")))],
        deck_name="Q1 Review",
        year="2026",
    )
    xml = _slides(out)[0]
    assert "Income" not in _text(xml)
    assert "Statement" not in _text(xml)
    assert xml.count("<a:gridCol") == 3


def test_branding_survives_the_clone(template: bytes) -> None:
    out = fill_template(
        template, [Slide("table", title="T", rows=(("A",), ("1",)))], deck_name="D", year="2026"
    )
    xml = _slides(out)[0]
    assert OMANTEL_BLUE in xml, "the brand blue is drawn per-shape and must be carried across"
    with zipfile.ZipFile(io.BytesIO(out)) as archive:
        assert any("/media/" in n for n in archive.namelist()), "logo and marks must survive"


def test_fonts_survive_the_clone(template: bytes) -> None:
    out = fill_template(template, [Slide("section", title="Adoption")], deck_name="D", year="2026")
    assert OMANTEL_FONT in _slides(out)[0]


def test_an_unknown_kind_is_refused(template: bytes) -> None:
    with pytest.raises(TemplateFillError):
        fill_template(template, [Slide("nonexistent")], deck_name="D", year="2026")


def test_an_empty_deck_is_refused(template: bytes) -> None:
    with pytest.raises(TemplateFillError):
        fill_template(template, [], deck_name="D", year="2026")


# --- the mapper -------------------------------------------------------------


class _Container:
    def __init__(self, heading: str = "", body: str = "", rows: list | None = None) -> None:
        self.heading, self.body, self.rows = heading, body, rows or []


class _Slide:
    def __init__(self, layout: str, title: str, containers: list) -> None:
        self.layout_type, self.slide_title, self.containers = layout, title, containers


class _Deck:
    def __init__(self, slides: list) -> None:
        self.slides = slides


def test_four_cards_pick_the_four_across_stencil() -> None:
    deck = _Deck([_Slide("3-column-cards", "T", [_Container(f"H{i}", f"B{i}") for i in range(4)])])
    assert deck_to_slides(deck)[0].kind == "points4"


def test_a_table_container_picks_the_table_stencil() -> None:
    deck = _Deck([_Slide("data-showcase", "T", [_Container(rows=[["a", "b"], ["1", "2"]])])])
    assert deck_to_slides(deck)[0].kind == "table"


def test_every_card_gets_a_label() -> None:
    """The 'inconsistent card structure' defect: a group where some cards have a
    title and others are a bare fragment.

    A card with only one sentence of content is label-only ON PURPOSE — writing
    that sentence into both slots is the duplication defect, so the two rules
    resolve in favour of never repeating text."""
    deck = _Deck([_Slide("process-flow", "Fallback title", [_Container(body="Only a body.")])])
    slot = deck_to_slides(deck)[0].slots[0]
    assert slot.label
    assert slot.label not in slot.body


def test_a_single_over_long_sentence_is_kept_whole() -> None:
    """There is no sentence boundary to break on, so the card keeps the whole
    thing and the filler scales the type. Cutting it would lose the end, which
    is the defect this replaced."""
    one_sentence = "Word " + "word " * 200
    deck = _Deck([_Slide("process-flow", "T", [_Container("H", one_sentence)])])
    slot = deck_to_slides(deck)[0].slots[0]
    assert "…" not in slot.body
    assert slot.body.startswith("Word word"), "the heading stands, the sentence is untouched"


def test_more_cards_than_the_stencil_holds_are_dropped() -> None:
    """Never a second '(cont.)' slide: the stencil's slot count wins."""
    deck = _Deck([_Slide("process-flow", "T", [_Container(f"H{i}", f"B{i}") for i in range(9)])])
    out = deck_to_slides(deck)
    assert len(out) == 1
    assert len(out[0].slots) <= 4


# --- v2: the defects a filled template introduced ---------------------------

# Sample content from the Omantel template. None of it may ever ship.
TEMPLATE_SAMPLE = (
    "+900",
    "100,000  users",
    "August 12 2011",
    "Internal Marketing",
    "Worldwide",
    "1 Million",
    "Headline goes here",
    "Presentation",
    "wonderful serenity",
)


def test_a_table_stencil_without_rows_never_ships_the_sample_table(template: bytes) -> None:
    """BLOCKER: a chart-less data slide kept the stencil's own table — "$50,000 /
    Worldwide / August 12 2011" under a heading of ours."""
    out = fill_template(
        template, [Slide("table", title="Agents by state")], deck_name="D", year="2026"
    )
    xml = _slides(out)[0]
    body = _text(xml)
    for sample in TEMPLATE_SAMPLE:
        assert sample not in body
    assert "<a:tbl>" not in xml, "the unfilled table frame must be removed, not left empty"


def test_no_template_sample_text_survives_a_full_deck(template: bytes) -> None:
    out = fill_template(
        template,
        [
            Slide("title", title="Q1 Review"),
            Slide("section", title="Adoption"),
            Slide("statement", title="A sovereign marketplace"),
            Slide("points", title="How it works", slots=(Slot("One", "First."),)),
            Slide("points4", title="Four", slots=tuple(Slot(f"L{i}", f"B{i}.") for i in range(4))),
            Slide("table", title="Hire or build", rows=(("A", "B"), ("1", "2"))),
        ],
        deck_name="Q1 Review",
        year="2026",
    )
    body = " ".join(_text(x) for x in _slides(out))
    for sample in TEMPLATE_SAMPLE:
        assert sample not in body, f"template sample content shipped: {sample!r}"


def test_body_copy_is_never_truncated_with_an_ellipsis() -> None:
    """BLOCKER: sentences ended mid-word ("Credentials are entered once into a…"),
    so the reader could not finish one. Overflow moves to the notes instead."""
    long_body = (
        "The builder states the job and Sila reflects the scope back. "
        "Nothing is created until the builder confirms that summary. "
        "Scope drift is therefore caught at the start rather than at review. "
        "A second reviewer sees the same summary before publication is possible. "
        "The audit trail keeps every version of the scope for later inspection."
    )
    deck = _Deck([_Slide("process-flow", "T", [_Container("H", long_body)])])
    out = deck_to_slides(deck)[0]
    assert "…" not in out.slots[0].body
    assert out.slots[0].body.endswith(".")
    assert out.notes, "the tail must reach the speaker notes rather than be lost"


def test_a_cont_title_never_reaches_a_slide() -> None:
    for raw in ("What this review covers (cont.)", "Risks (continued)", "Scope (Cont.)"):
        deck = _Deck([_Slide("quote-focus", raw, [])])
        assert "cont" not in deck_to_slides(deck)[0].title.lower()


def test_the_slide_title_is_never_reused_as_a_card_label() -> None:
    """A node label that just repeats the slide's heading is the title pasted
    into a content slot."""
    deck = _Deck(
        [
            _Slide(
                "process-flow",
                "What this review covers",
                [_Container("What this review covers", "The scope of the quarter. And the ask.")],
            )
        ]
    )
    assert deck_to_slides(deck)[0].slots[0].label != "What this review covers"


class _ChartContainer:
    def __init__(self, chart: object) -> None:
        self.chart, self.heading, self.body, self.rows = chart, "", "", []


def test_a_chart_container_picks_the_chart_stencil() -> None:
    deck = _Deck([_Slide("data-showcase", "Agents by state", [_ChartContainer(object())])])
    assert deck_to_slides(deck)[0].kind == "chart"


def test_a_chart_becomes_a_native_editable_part(template: bytes) -> None:
    """Not a picture: a real c:chart part the reader can edit in PowerPoint."""
    from sila.services.slides.schema import ChartSpec

    spec = ChartSpec.model_validate(
        {
            "kind": "bar",
            "categories": ["Published", "Draft"],
            "series": [{"name": "Agents", "values": [12, 8]}],
        }
    )
    out = fill_template(
        template,
        [Slide("chart", title="Agents by state", chart=spec)],
        deck_name="D",
        year="2026",
    )
    with zipfile.ZipFile(io.BytesIO(out)) as archive:
        names = archive.namelist()
        assert "ppt/charts/chart1.xml" in names
        chart = archive.read("ppt/charts/chart1.xml").decode()
        rels = archive.read("ppt/slides/_rels/slide1.xml.rels").decode()
    assert "chart1.xml" in rels, "the slide must reference the chart part"
    assert 'showVal val="1"' in chart, "data labels on"
    assert OMANTEL_HIGHLIGHT in chart, "the leading point takes the brand orange"
    assert "2D13EA" in chart or OMANTEL_BLUE in chart, "series draw from the brand blue"
    assert not any(n.endswith((".png", ".jpg")) and "charts" in n for n in names)


def test_notes_are_attached_as_a_real_notes_part(template: bytes) -> None:
    out = fill_template(
        template,
        [Slide("section", title="Adoption", notes="The detail that did not fit.")],
        deck_name="D",
        year="2026",
    )
    with zipfile.ZipFile(io.BytesIO(out)) as archive:
        assert "ppt/notesSlides/notesSlide1.xml" in archive.namelist()
        assert "did not fit" in archive.read("ppt/notesSlides/notesSlide1.xml").decode()
        assert "notesSlide1.xml" in archive.read("ppt/slides/_rels/slide1.xml.rels").decode()


# --- v3: the slicing root cause ---------------------------------------------


class _Chunked:
    """A slide shaped the way `schema` emits a chunked body: the first chunk
    keeps the (often empty) heading, every later chunk is headless."""

    def __init__(self, title: str, bodies: list[str], layout: str = "3-column-cards") -> None:
        self.layout_type, self.slide_title = layout, title
        self.containers = [_Container("", b) for b in bodies]


def test_a_sliced_sentence_is_reassembled_into_one_item() -> None:
    """ROOT CAUSE: one sentence arrived as eight fragments, each reading as a
    heading. Headless siblings are continuations, not items."""
    deck = _Deck(
        [
            _Chunked(
                "What this review covers",
                ["Scope of this review.", "What Sila is, how agents", "are specified and vetted."],
            )
        ]
    )
    out = deck_to_slides(deck)
    assert len(out) == 1
    assert len(out[0].slots) == 1, "three fragments are one authored item"
    slot = out[0].slots[0]
    assert slot.body.endswith(".")
    assert "how agents are specified and vetted" in f"{slot.label} {slot.body}"


def test_a_continuation_page_is_folded_back() -> None:
    """Four consecutive slides titled 'Q2 roadmap and the ask' were the
    paginator running out of slots, never a deliberate continuation."""
    deck = _Deck(
        [
            _Chunked("Q2 roadmap", ["Scope of this review."]),
            _Chunked("Q2 roadmap (cont.)", ["What Sila is,"]),
            _Chunked("Q2 roadmap (cont.)", ["and how agents are vetted."]),
        ]
    )
    out = deck_to_slides(deck)
    assert len(out) == 1
    assert out[0].title == "Q2 roadmap"


def test_a_page_break_falling_mid_item_still_joins() -> None:
    deck = _Deck(
        [
            _Chunked("Scope", ["Sila is a marketplace"]),
            _Chunked("Scope (cont.)", ["for vetted agents."]),
        ]
    )
    slot = deck_to_slides(deck)[0].slots[0]
    assert "marketplace for vetted agents" in f"{slot.label} {slot.body}"


def test_no_string_is_written_into_two_shapes() -> None:
    """The label was the body's first sentence AND the body kept it, so every
    fragment appeared twice."""
    deck = _Deck([_Chunked("T", ["A complete first sentence. And a second one here."])])
    slot = deck_to_slides(deck)[0].slots[0]
    assert slot.label not in slot.body


def test_no_slide_title_appears_twice() -> None:
    deck = _Deck(
        [
            _Slide("3-column-cards", "Same title", [_Container("A", "One.")]),
            _Slide("3-column-cards", "Same title", [_Container("B", "Two.")]),
        ]
    )
    titles = [s.title for s in deck_to_slides(deck)]
    assert len(titles) == len(set(titles))


def test_the_cover_survives_content_hung_off_it() -> None:
    """v3 opened on a content slide: a title-hero with containers was converted
    to a card layout, so the deck had no cover."""
    deck = _Deck([_Slide("title-hero", "Sila — Q1 Review", [_Container("", "Scope of this.")])])
    out = deck_to_slides(deck)
    assert out[0].kind == "title"
    assert out[0].notes, "the hung content moves to the notes rather than vanishing"


def test_all_numeric_items_become_kpi_blocks() -> None:
    """'Adoption so far' was four diamonds of prose with the numbers left out.

    A metrics slide STATES its figures — large, with the descriptor beneath —
    rather than graphing them; a data slide graphs them."""
    deck = _Deck(
        [
            _Slide(
                "metric-showcase",
                "Adoption so far",
                [
                    _Container(name, str(value))
                    for name, value in (("Defined", 25), ("Published", 12), ("Draft", 8))
                ],
            )
        ]
    )
    out = deck_to_slides(deck)[0]
    assert out.kind == "kpi"
    assert [slot.label for slot in out.slots] == ["25", "12", "8"]
    assert [slot.body for slot in out.slots] == ["Defined", "Published", "Draft"]


def test_no_layout_is_used_more_than_twice() -> None:
    """Four identical diamond timelines is a deck of two alternating stencils."""
    deck = _Deck(
        [
            _Slide("3-column-cards", f"Slide {i}", [_Container(f"H{i}", "A sentence.")])
            for i in range(5)
        ]
    )
    kinds = [s.kind for s in deck_to_slides(deck)]
    assert max(kinds.count(k) for k in set(kinds)) <= 3, kinds


def test_a_sequence_layout_is_only_used_for_sequential_content() -> None:
    """A timeline implies an order; "the problem it solves" is four parallel
    problems, not four steps."""
    parallel = _Deck(
        [
            _Slide(
                "3-column-cards",
                "The problem it solves",
                [_Container(f"H{i}", "A sentence.") for i in range(3)],
            )
        ]
    )
    sequential = _Deck(
        [
            _Slide(
                "3-column-cards",
                "How an agent is built",
                [_Container(f"H{i}", "A sentence.") for i in range(3)],
            )
        ]
    )
    assert deck_to_slides(parallel)[0].kind == "points4"
    assert deck_to_slides(sequential)[0].kind == "points"


def test_a_label_running_into_its_body_is_rejoined() -> None:
    """ "No visibility of what is" / "live. Without a register…" is one sentence
    the model split across the two fields."""
    deck = _Deck(
        [
            _Slide(
                "3-column-cards",
                "T",
                [_Container("No visibility of what is", "live. Without a register there is none.")],
            )
        ]
    )
    slot = deck_to_slides(deck)[0].slots[0]
    # No terminal stop on a label: it is a heading, not a sentence.
    assert slot.label == "No visibility of what is live"
    assert slot.body == "Without a register there is none."


def test_prose_is_never_coerced_into_a_chart() -> None:
    deck = _Deck(
        [_Slide("metric-showcase", "T", [_Container("A", "Not a number."), _Container("B", "12")])]
    )
    assert deck_to_slides(deck)[0].kind != "chart"


def test_more_items_than_slots_go_to_notes_not_a_repeated_slide() -> None:
    deck = _Deck(
        [_Slide("3-column-cards", "Roadmap", [_Container(f"H{i}", f"Item {i}.") for i in range(7)])]
    )
    out = deck_to_slides(deck)
    assert len(out) == 1, "never a second slide wearing the same title"
    assert len(out[0].slots) <= 4
    assert "H6" in out[0].notes


def test_no_empty_slot_is_emitted() -> None:
    deck = _Deck([_Slide("3-column-cards", "T", [_Container("A", "One."), _Container("", "")])])
    slots = deck_to_slides(deck)[0].slots
    assert all(s.label or s.body for s in slots)


def test_a_table_cell_is_never_cut_mid_phrase() -> None:
    """'dedup and rate' — a cell cut mid-phrase is unreadable."""
    long_cell = "Steps, input and output per step, connectors, and success criteria. Extra detail."
    deck = _Deck([_Slide("data-showcase", "T", [_Container(rows=[["A"], [long_cell]])])])
    cell = deck_to_slides(deck)[0].rows[1][0]
    assert "…" not in cell
    assert cell.endswith(".")


# --- v4: title/body pairing, table legibility, chart identification ----------


def test_a_title_never_drifts_from_its_content() -> None:
    """BLOCKER: merging a "(cont.)" page shortened the section list but not the
    raw slide list, so every LATER title sat above the next slide's content — a
    risk matrix under a heading reading "Q2 roadmap"."""
    deck = _Deck(
        [
            _Chunked("Scope", ["Sila is a marketplace"]),
            _Chunked("Scope (cont.)", ["for vetted agents."]),
            _Slide("data-showcase", "Hire or build", [_Container(rows=[["A", "B"], ["1", "2"]])]),
            _Slide(
                "data-showcase", "Risks", [_Container(rows=[["Risk", "Fix"], ["Drift", "Vet"]])]
            ),
        ]
    )
    out = deck_to_slides(deck)
    by_title = {s.title: s for s in out}
    assert by_title["Hire or build"].rows[0] == ("A", "B")
    assert by_title["Risks"].rows[0] == ("Risk", "Fix")


def test_table_body_text_is_legible_at_presentation_distance(template: bytes) -> None:
    """~9pt stencil type is unreadable from the back of a room."""
    out = fill_template(
        template,
        [
            Slide(
                "table",
                title="Hire or build",
                # Three body rows, so alternate banding actually has a row to land on.
                rows=(
                    ("Dimension", "Bazaar"),
                    ("Time", "Minutes."),
                    ("Review", "Already passed."),
                    ("Owner", "The publishing department."),
                ),
            )
        ],
        deck_name="D",
        year="2026",
    )
    xml = _slides(out)[0]
    sizes = {int(n) for n in re.findall(r'sz="(\d+)"', xml)}
    table_sizes = {n for n in sizes if n in (1400, 1600)}
    assert table_sizes, "body 14pt / header 16pt must be set on the table"
    assert 'algn="l"' in xml, "cells left-align so multi-line text scans"
    assert "F2F1FD" in xml, "light banding helps the eye track a wide row"


def test_a_chart_names_every_bar(template: bytes) -> None:
    from sila.services.slides.schema import ChartSpec

    spec = ChartSpec.model_validate(
        {
            "kind": "bar",
            "categories": ["Published and available", "Still draft"],
            "series": [{"name": "Agents", "values": [12, 8]}],
        }
    )
    out = fill_template(
        template, [Slide("chart", title="Adoption", chart=spec)], deck_name="D", year="2026"
    )
    with zipfile.ZipFile(io.BytesIO(out)) as archive:
        chart = archive.read("ppt/charts/chart1.xml").decode()
    assert "<c:legend" in chart, "four differently-coloured bars need naming"
    assert 'varyColors val="1"' in chart, "the legend lists POINTS, not the single series"
    assert 'barDir val="bar"' in chart, "long category names want horizontal bars"
    for colour in ("2D13EA", "FF7800"):
        assert colour in chart


def test_no_total_is_ever_invented() -> None:
    """A breakdown may or may not already include its own total: "25 registered"
    beside 12 / 3 / 8 sums to 48, which describes nothing, and 12 / 3 / 8 alone
    is indistinguishable from it by the numbers. A wrong figure on a board slide
    is worse than none, so the total stays the model's to author."""
    for containers in (
        [_Container("Published", "12"), _Container("In review", "3"), _Container("Draft", "8")],
        [_Container("Registered", "25"), _Container("Published", "12"), _Container("Draft", "8")],
    ):
        deck = _Deck([_Slide("metric-showcase", "Adoption so far", containers)])
        assert "total" not in deck_to_slides(deck)[0].title.lower()


# --- v6: licence-bearing imagery --------------------------------------------


def _synthetic_asset(width: int = 2400, height: int = 1000) -> object:
    """An on-brand image built locally, so these tests never hit the network."""
    import io as _io

    from PIL import Image, ImageDraw

    from sila.services.slides.imagery import ImageAsset

    image = Image.new("RGB", (width, height), "#0B0B2E")
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 40):
        draw.line([(x, 0), (x - 400, height)], fill="#2D13EA", width=3)
    buffer = _io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return ImageAsset(
        data=buffer.getvalue(),
        mime="image/jpeg",
        width=width,
        height=height,
        title="Network lines",
        source_url="https://example.org/file",
        author="A. Author",
        licence="CC0",
        licence_url="https://creativecommons.org/publicdomain/zero/1.0/",
    )


def test_a_commercial_licence_is_required() -> None:
    from sila.services.slides.imagery import _licence_allowed

    for allowed in ("CC0", "Public domain", "CC BY 4.0", "CC BY-SA 3.0", "Unsplash Licence"):
        assert _licence_allowed(allowed), allowed
    # NonCommercial and NoDerivatives cannot go in a deck an enterprise presents.
    for blocked in ("CC BY-NC 4.0", "CC BY-NC-SA 2.0", "CC BY-ND 2.0", "All rights reserved"):
        assert not _licence_allowed(blocked), blocked


def test_off_palette_images_are_rejected() -> None:
    """Green and teal clash with the brand however good the picture is."""
    import io as _io

    from PIL import Image

    from sila.services.slides.imagery import palette_ok

    def solid(colour: str) -> bytes:
        buffer = _io.BytesIO()
        Image.new("RGB", (400, 300), colour).save(buffer, "JPEG")
        return buffer.getvalue()

    assert palette_ok(solid("#2D13EA"))  # brand blue
    assert palette_ok(solid("#FF7800"))  # brand orange
    assert palette_ok(solid("#9A9AA5"))  # neutral
    assert not palette_ok(solid("#1E9E5A"))  # green
    assert not palette_ok(solid("#12B5B0"))  # teal


def test_an_image_is_embedded_never_linked(template: bytes) -> None:
    """A linked picture breaks the moment the deck is emailed; this one has to
    render with no network at all."""
    out = fill_template(
        template,
        [Slide("section", title="The current state", image=_synthetic_asset())],
        deck_name="D",
        year="2026",
    )
    with zipfile.ZipFile(io.BytesIO(out)) as archive:
        assert any("media/silaImage" in n for n in archive.namelist())
        slide = archive.read("ppt/slides/slide1.xml").decode()
        rels = archive.read("ppt/slides/_rels/slide1.xml.rels").decode()
    assert "r:embed" in slide and "r:link" not in slide
    assert 'TargetMode="External"' not in rels


def test_an_image_is_cropped_not_stretched(template: bytes) -> None:
    out = fill_template(
        template,
        [Slide("section", title="T", image=_synthetic_asset(2400, 1000))],
        deck_name="D",
        year="2026",
    )
    assert "<a:srcRect" in _slides(out)[0], "aspect is preserved by cropping the source"


def test_an_image_is_skipped_where_a_stencil_has_no_room(template: bytes) -> None:
    """Never floated over the designed content — no room means no image."""
    out = fill_template(
        template,
        [Slide("points", title="T", slots=(Slot("A", "One."),), image=_synthetic_asset())],
        deck_name="D",
        year="2026",
    )
    with zipfile.ZipFile(io.BytesIO(out)) as archive:
        assert not any("media/silaImage" in n for n in archive.namelist())


def test_every_image_is_credited_in_the_deck(template: bytes) -> None:
    """Source, author and licence travel WITH the file, so it can be audited
    later without going back to the generator."""
    out = fill_template(
        template,
        [Slide("section", title="T", image=_synthetic_asset())],
        deck_name="D",
        year="2026",
    )
    with zipfile.ZipFile(io.BytesIO(out)) as archive:
        notes = archive.read("ppt/notesSlides/notesSlide1.xml").decode()
    for fragment in ("CC0", "A. Author", "example.org"):
        assert fragment in notes


def test_guard_rejection_is_not_the_schema_error(template: bytes) -> None:
    """Two classes share the name DeckValidationError: the schema's (ValueError)
    and deck_guard's (RuntimeError). Neither inherits the other, so a caller
    catching the wrong one lets a guard rejection escape as an unhandled crash —
    which is how a rejected deck reached the user as a bare "Failed" with the
    reason left in the log. If these ever merge, the alias at the catch site in
    tool_registry can go."""
    from sila.services.slides import schema as slide_schema
    from sila.services.slides.deck_guard import DeckValidationError as GuardRejection

    assert GuardRejection is not slide_schema.DeckValidationError
    assert not issubclass(GuardRejection, slide_schema.DeckValidationError)
    assert not issubclass(slide_schema.DeckValidationError, GuardRejection)


def test_a_dense_deck_builds_instead_of_being_rejected(template: bytes) -> None:
    """Four rejections in a row came from one mismatch: the mapper allowed 320
    characters a card, the stencil holds 110-150, so the renderer scaled the
    type below the guard's 12pt floor and the guard stopped it — correctly, on
    content that should never have reached it. It builds first time now."""
    from sila.services.slides import schema as slide_schema

    body = (
        "Ember is the source. IRENA supplies capacity. The IEA adds market "
        "context. Oman comes from the regulator. Discrepancies are footnoted. "
        "Nothing is reconciled silently. The appendix carries the workings."
    )
    deck, _ = slide_schema.validate_deck(
        {
            "title": "Renewable Electricity Generation",
            "palette": "modern-slate",
            "slides": [
                {"layoutType": "title-hero", "slideTitle": "Renewable generation"},
                {
                    "layoutType": "3-column-cards",
                    "slideTitle": "Method and sources",
                    "containers": [
                        {"type": "card", "heading": "Sources", "body": body},
                        {"type": "card", "heading": "Window", "body": body},
                        {"type": "card", "heading": "Caveats", "body": body},
                    ],
                },
            ],
        }
    )
    book = stencil_book_for(template)
    mapped = deck_to_slides(deck, stencils=book)
    out = fill_template(template, mapped, deck_name="Renewables", year="2026", stencils=book)
    assert len(out) > 100_000, "the deck built"
    content = next(slide for slide in mapped if slide.slots)
    budget = book[content.kind].slot_chars
    for slot in content.slots:
        assert len(slot.body) <= budget, f"{len(slot.body)} > {budget}: {slot.body}"
    assert content.notes, "the sentences that did not fit reach the notes"


def test_a_card_is_budgeted_against_its_own_stencil() -> None:
    """The budget is the stencil's, not a fixed 320. Whole sentences only — a
    sentence the reader cannot finish has lost its information — so a single
    sentence longer than the card is kept and the renderer scales it instead."""
    from sila.services.slides.template_map import _slot

    many = "One fits. Two fits. Three fits. Four fits. Five fits. Six fits here."
    slot, overflow = _slot("Label", many, budget=40)
    assert len(slot.body) <= 40 and overflow, (slot.body, overflow)
    assert slot.body in many and overflow in many, "split, never rewritten"

    one_long = "A single sentence that is comfortably longer than the card can hold."
    slot, overflow = _slot("Label", one_long, budget=40)
    assert slot.body == one_long and overflow == "", "a lone sentence is not cut"


def test_the_shrinker_never_goes_below_the_guards_floor() -> None:
    """The renderer's shrinker floored at 10pt while the guard rejects under
    12pt, so an overlong card could only ever produce a deck the guard was
    certain to reject."""
    from xml.etree import ElementTree as ET

    from sila.services.slides.deck_guard import MIN_BODY_PT
    from sila.services.slides.template_fill import _shrink_to_fit

    shape = ET.fromstring('<sp xmlns="x"><rPr sz="1800"/><rPr sz="1400"/></sp>')
    _shrink_to_fit(shape, "x" * 4000, 40)  # far past any budget
    sizes = [int(n.get("sz")) for n in shape.iter() if n.get("sz")]
    assert sizes and min(sizes) >= MIN_BODY_PT, sizes


def test_a_table_may_have_more_columns_than_the_stencil(template: bytes) -> None:
    """The stencil draws five columns; a comparison of four rivals needs six.

    Before this, `_trim_columns` only ever shrank, so the sixth column was
    silently dropped along with whatever it compared.
    """
    rows = (
        ("Feature", "Sila", "A", "B", "C", "D"),
        ("Does a thing\nand explains it", "✓", "◐", "✕", "✓", "✕"),
    )
    out = fill_template(
        template,
        [Slide("table", title="Compare", rows=rows, table_highlight_col=2, table_center_from=2)],
        deck_name="Deck",
        year="2026",
    )
    xml = _slides(out)[0]
    assert xml.count("<a:gridCol") == 6, "the sixth column was dropped"
    for value in ("Feature", "Sila", "A", "B", "C", "D"):
        assert value in _text(xml)


def test_a_cell_can_carry_a_bold_name_over_a_plain_line(template: bytes) -> None:
    """A comparison row is a name the reader scans for and a sentence they read
    once they have stopped, so the cell holds both at different weights."""
    rows = (
        ("Feature", "Sila"),
        ("Hands back finished work\nDecks, Word, PDF and Excel.", "✓"),
    )
    out = fill_template(
        template, [Slide("table", title="Compare", rows=rows)], deck_name="Deck", year="2026"
    )
    body = _text(_slides(out)[0])
    assert "Hands back finished work" in body
    assert "Decks, Word, PDF and Excel." in body
    assert "\n" not in body, "the newline is a formatting instruction, not content"


def test_comparison_marks_are_coloured_by_what_they_mean(template: bytes) -> None:
    """A mark is read as a colour before it is read as a glyph."""
    rows = (("Feature", "Sila", "Other"), ("A thing", "✓", "✕"))
    out = fill_template(
        template,
        [Slide("table", title="Compare", rows=rows, table_center_from=2)],
        deck_name="Deck",
        year="2026",
    )
    xml = _slides(out)[0]
    assert "2C12E7" in xml, "a full mark is not drawn in the brand blue"
    assert "9A9AA5" in xml, "an absent mark is not drawn grey"


def test_the_table_stencils_lower_sparks_do_not_sit_on_the_table(template: bytes) -> None:
    """The stencil parks two diamonds at x=11.16 and x=11.74, y=6.0+ — inside
    the footprint of a table spanning 0.62in to 12.65in. Any table past four
    rows reaches them, and they land on its last column."""
    rows = (("Feature", "Sila"),) + tuple((f"Row {i}", "✓") for i in range(1, 9))
    out = fill_template(
        template, [Slide("table", title="Compare", rows=rows)], deck_name="Deck", year="2026"
    )
    xml = _slides(out)[0]
    assert xml.count('name="object 4"') <= 1, "a decorative spark still overlaps the table"
