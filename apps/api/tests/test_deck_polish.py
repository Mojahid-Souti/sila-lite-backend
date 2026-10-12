"""The defects one rendered retention deck exposed, pinned.

Every one of these was visible in a PDF the user opened: a KPI figure wrapped
onto three lines and pushed its descriptor off the slide, an unused slot left
an empty diamond at the end of the chain, the same tap icon sat in every frame
(and a house in one), the running header wrapped, twelve chart labels collided,
and a Grafana push charted the mail export instead of the performance file.
"""

import io
import pathlib
import uuid
import zipfile
from xml.etree import ElementTree as ET

import pytest

from sila.services.slides import schema as slide_schema
from sila.services.slides.template_fill import (
    _clip_header,
    _figure_pt,
    _style_chart,
    fill_template,
    stencil_book_for,
)
from sila.services.slides.template_map import _compact, deck_to_slides

TEMPLATE = pathlib.Path(__file__).parents[1] / "sila/services/slides/templates/omantel.pptx"
# The designed corporate template is not part of every edition.
pytestmark = pytest.mark.skipif(not TEMPLATE.is_file(), reason="corporate .pptx template absent")
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"


@pytest.fixture(scope="module")
def template() -> bytes:
    return TEMPLATE.read_bytes()


def _slide_xml(deck: bytes, number: int) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(deck)) as archive:
        return ET.fromstring(archive.read(f"ppt/slides/slide{number}.xml"))


def _render(template: bytes, slides: list[dict]) -> bytes:
    deck, _ = slide_schema.validate_deck(
        {
            "title": "Retention Pack — Weekly Escalations & Performance",
            "palette": "modern-slate",
            "slides": slides,
        }
    )
    book = stencil_book_for(template)
    return fill_template(
        template,
        deck_to_slides(deck, stencils=book),
        deck_name=deck.title,
        year="2026",
        stencils=book,
    )


# --- figures ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("125,194.71", "125.2K"),
        ("1,472.88", "1.5K"),
        ("47.31", "47.3"),
        ("914.79", "915"),
        ("5", "5"),
        ("2,300,000", "2.3M"),
        ("18.40%", "18.4%"),
        ("Last 7 days", "Last 7 days"),  # not a number: untouched
    ],
)
def test_kpi_figures_are_compacted(raw: str, shown: str) -> None:
    assert _compact(raw) == shown


def test_longer_figures_get_smaller_never_larger() -> None:
    sizes = [_figure_pt("9" * n, 8000) for n in range(1, 14)]
    assert sizes == sorted(sizes, reverse=True)
    assert sizes[0] == 8000 and sizes[-1] < 4000


def test_a_kpi_row_shares_one_size_and_none_wraps(template: bytes) -> None:
    """Four figures of different lengths render at ONE point size — the size the
    longest needs — so they read as a set. That size must be small enough for
    "125.2K" to hold on one line in a 2.6in column (measured: 80pt does not)."""
    deck = _render(
        template,
        [
            {"layoutType": "title-hero", "slideTitle": "Retention pack"},
            {
                "layoutType": "metric-showcase",
                "slideTitle": "Base, value and usage",
                "containers": [
                    {"type": "metric", "heading": "Avg subscribers", "body": "125,194.71"},
                    {"type": "metric", "heading": "Avg revenue (OMR 000)", "body": "1,472.88"},
                    {"type": "metric", "heading": "Avg ARPU (OMR)", "body": "47.31"},
                    {"type": "metric", "heading": "Avg data traffic (TB)", "body": "914.79"},
                ],
            },
        ],
    )
    root = _slide_xml(deck, 2)
    figures = {}
    for sp in root.iter(f"{{{P}}}sp"):
        first = sp.find(f".//{{{A}}}p")
        if first is None:
            continue
        text = "".join(t.text or "" for t in first.iter(f"{{{A}}}t")).strip()
        if text in {"125.2K", "1.5K", "47.3", "915"}:
            sizes = {int(r.get("sz")) for r in first.iter(f"{{{A}}}rPr") if r.get("sz")}
            figures[text] = sizes
    assert set(figures) == {"125.2K", "1.5K", "47.3", "915"}
    all_sizes = set().union(*figures.values())
    assert len(all_sizes) == 1, f"row is not one size: {figures}"
    assert next(iter(all_sizes)) <= 5200


# --- frames and icons -------------------------------------------------------


def _custom_shapes(root: ET.Element) -> list[ET.Element]:
    return [
        sp
        for sp in root.iter(f"{{{P}}}sp")
        if sp.find(f"./{{{P}}}spPr/{{{A}}}custGeom") is not None
    ]


def _names(root: ET.Element) -> set[str]:
    return {sp.find(f".//{{{P}}}cNvPr").get("name") for sp in root.iter(f"{{{P}}}sp")}


def test_three_items_on_the_four_up_stencil_leave_no_empty_diamond(template: bytes) -> None:
    """Slide 7 of the template (measured): diamonds are 'object 12/13/14/15',
    the fourth column's connectors 'object 17/19', and 'object 21' is the ONE
    shape holding all four tap icons. Three items keep three diamonds and drop
    the icon shape."""
    deck = _render(
        template,
        [
            {"layoutType": "title-hero", "slideTitle": "Retention pack"},
            {
                "layoutType": "metric-showcase",
                "slideTitle": "This week's escalations",
                "containers": [
                    {"type": "metric", "heading": "Volume", "body": "5"},
                    {"type": "metric", "heading": "Window", "body": "Last 7 days"},
                    {"type": "metric", "heading": "Source", "body": "Inbox keyword match"},
                ],
            },
        ],
    )
    root = _slide_xml(deck, 2)
    names = _names(root)
    assert {"object 12", "object 13", "object 14"} <= names
    assert "object 15" not in names, "the fourth, empty diamond survived"
    assert {"object 17", "object 19"}.isdisjoint(names), "dangling connectors"
    # The icon shape is the one custom geometry with sixteen paths. Checked by
    # geometry, not name: the title borrowed from slide 8 is ALSO "object 21".
    icons = [sp for sp in _custom_shapes(root) if len(list(sp.iter(f"{{{A}}}path"))) >= 16]
    assert icons == [], "the tap icons survived"


def test_timeline_frames_are_uniform_with_no_icons(template: bytes) -> None:
    """Slide 8 (measured): 'object 24' is the filled house in the first diamond,
    'object 19'/'object 20' the tap icons in the other two. All three go; the
    three diamonds ('object 2/3/4') stay."""
    from sila.services.slides.template_fill import Slide, Slot

    book = stencil_book_for(template)
    deck = fill_template(
        template,
        [
            Slide("title", title="Retention pack"),
            Slide(
                "points",
                title="Read with care",
                slots=(
                    Slot("Sample size", "12 monthly points."),
                    Slot("Standing ask", "Desk lead signs off."),
                    Slot("Open item", "Confirm the filter rule."),
                ),
            ),
        ],
        deck_name="T",
        year="2026",
        stencils=book,
    )
    names = _names(_slide_xml(deck, 2))
    assert {"object 2", "object 3", "object 4"} <= names
    assert {"object 19", "object 20", "object 24"}.isdisjoint(names), names


# --- header -----------------------------------------------------------------


def test_running_header_is_clipped_not_wrapped() -> None:
    assert _clip_header("Retention Pack — Weekly Escalations & Performance") == (
        "Retention Pack — Weekly Escalations & Performance"
    )
    long = "A very long deck name that goes on and on past what any header can hold"
    out = _clip_header(long)
    assert out.endswith("…") and len(out) <= 52


# --- chart labels -----------------------------------------------------------


def _line_chart_xml(n: int) -> str:
    pts = "".join(
        f'<c:pt idx="{i}"><c:v>{100 + i * (3 if i != 6 else 30)}</c:v></c:pt>' for i in range(n)
    )
    cats = "".join(f'<c:pt idx="{i}"><c:v>m{i}</c:v></c:pt>' for i in range(n))
    return (
        '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:chart><c:plotArea><c:lineChart>'
        '<c:ser><c:idx val="0"/><c:order val="0"/>'
        f"<c:cat><c:strRef><c:strCache>{cats}</c:strCache></c:strRef></c:cat>"
        f"<c:val><c:numRef><c:numCache>{pts}</c:numCache></c:numRef></c:val></c:ser>"
        "</c:lineChart></c:plotArea></c:chart></c:chartSpace>"
    )


def test_dense_series_keeps_first_peak_and_last_labels_only() -> None:
    cq = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
    root = ET.fromstring(_style_chart(_line_chart_xml(12)))
    deleted = {
        int(d.find(f"{cq}idx").get("val"))
        for d in root.iter(f"{cq}dLbl")
        if d.find(f"{cq}delete") is not None
    }
    assert deleted == set(range(12)) - {0, 6, 11}  # index 6 is the peak


def test_sparse_series_keeps_every_label() -> None:
    cq = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
    root = ET.fromstring(_style_chart(_line_chart_xml(6)))
    assert list(root.iter(f"{cq}dLbl")) == []


# --- which data file --------------------------------------------------------


def test_grafana_follows_the_dashboard_then_the_attachment_then_recency() -> None:
    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext, _pick_data_file, _stash_virtual_file

    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )
    attached = {"name": "omantel-demo-revenue-2025.csv", "content": "a,b\n1,2"}
    ctx.build_state["uploaded_files"] = [attached]
    _stash_virtual_file(ctx, "outlook-emails.csv", "from,subject\nx,y")  # newer, virtual
    files = ctx.build_state["uploaded_files"]
    # The mail export is newest, but the user's attachment wins.
    assert _pick_data_file(ctx, files)["name"] == "omantel-demo-revenue-2025.csv"
    # Once a dashboard was built from a file, everything follows THAT file.
    ctx.build_state["dashboard_source"] = "outlook-emails.csv"
    assert _pick_data_file(ctx, files)["name"] == "outlook-emails.csv"
    # Only virtual files present → most recent.
    ctx.build_state.pop("dashboard_source")
    _stash_virtual_file(ctx, "teams-messages.txt", "hi")
    assert (
        _pick_data_file(ctx, [f for f in files if f.get("virtual")])["name"] == "teams-messages.txt"
    )
