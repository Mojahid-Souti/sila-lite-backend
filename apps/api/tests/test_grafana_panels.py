"""The four defects one pushed Grafana dashboard showed at once.

From the screenshot: a sawtooth where a revenue trend should be, three metrics
of wildly different magnitude on one axis, a panel stuck on "Loading plugin
panel…", and a stacked bar reading "100 / 91.7 / 400". The data behind it is
the real demo file — 12 months x 4 governorates x 2 segments = 96 rows.
"""

import pathlib

import pytest

from sila.services.connectors.grafana_dashboard import build_dashboard_with_data
from sila.services.sila.data_dashboard import _AXIS_RATIO, build_data_dashboard

CSV = pathlib.Path(__file__).parents[3] / "out/omantel-demo-revenue-2025.csv"
# A local demo file (out/ is not committed): the suite skips where it is absent.
pytestmark = pytest.mark.skipif(not CSV.is_file(), reason="demo CSV not present")


@pytest.fixture(scope="module")
def charts() -> list[dict]:
    return build_data_dashboard("omantel-demo-revenue-2025.csv", CSV.read_text())["charts"]


@pytest.fixture(scope="module")
def panels(charts: list[dict]) -> list[dict]:
    return build_dashboard_with_data("Weekly pack", charts, testdata_uid="t")["panels"]


def test_a_month_appears_once_per_line_not_once_per_row(charts: list[dict]) -> None:
    """96 rows over 12 months means every month carries eight values, from 606
    to 3,743. Plotted unaggregated in date order that is a sawtooth between the
    smallest and largest region, which is what shipped."""
    lines = [c for c in charts if c["kind"] == "line"]
    assert lines, "the file has a date column; it should chart a trend"
    for line in lines:
        months = [p["x"] for p in line["data"]]
        assert len(months) == 12, f"{line['title']}: {len(months)} points for 12 months"
        assert months == sorted(months)
        assert len(set(months)) == len(months), "a month must not repeat"


def test_the_trend_rises_instead_of_zig_zagging(charts: list[dict]) -> None:
    """The demo data climbs all year. A sawtooth shows as a huge swing between
    neighbouring points; the aggregated series should move smoothly."""
    revenue = next(c for c in charts if c["kind"] == "line" and c["series"] == ["revenue_omr_000"])
    values = [p["revenue_omr_000"] for p in revenue["data"]]
    assert values[-1] > values[0], "the year should end higher than it started"
    biggest_step = max(abs(b - a) for a, b in zip(values, values[1:], strict=False))
    assert biggest_step < (max(values) - min(values)), "no single step spans the whole range"


def test_metrics_of_different_magnitude_get_their_own_axis(charts: list[dict]) -> None:
    """Subscribers near 400,000 and revenue near 2,700 on one axis renders
    revenue as a flat line on zero. Each gets its own chart."""
    lines = [c for c in charts if c["kind"] == "line"]
    for line in lines:
        peaks = [max(abs(p[name]) for p in line["data"] if name in p) for name in line["series"]]
        if len(peaks) > 1:
            assert max(peaks) <= min(peaks) * _AXIS_RATIO, f"{line['title']} mixes magnitudes"
    charted = {name for line in lines for name in line["series"]}
    assert {"revenue_omr_000", "subscribers"} <= charted, "both metrics still shown"
    assert len(lines) > 1, "they cannot share one axis, so there is more than one chart"


def test_every_panel_is_a_core_grafana_type(panels: list[dict]) -> None:
    """Grafana 11.3's core panels. Anything else sits on "Loading plugin panel…"
    forever, which is how a scatter panel shipped blank."""
    core = {
        "timeseries",
        "barchart",
        "piechart",
        "stat",
        "table",
        "trend",
        "xychart",
        "gauge",
        "bargauge",
        "histogram",
        "heatmap",
        "text",
    }
    assert {p["type"] for p in panels} <= core


def test_an_xychart_carries_the_field_mapping_it_needs(charts: list[dict]) -> None:
    """Grafana 11's xychart never initialises without a series mapping — the
    panel hangs on "Loading plugin panel…". `x`/`y` are the columns the CSV
    writer emits."""
    scatter = {
        "kind": "scatter",
        "title": "subscribers vs revenue",
        "data": [{"x": 1, "y": 2}, {"x": 3, "y": 4}],
    }
    panel = build_dashboard_with_data("S", [scatter], testdata_uid="t")["panels"][0]
    assert panel["type"] == "xychart"
    series = panel["options"]["series"][0]
    assert series["x"]["matcher"]["options"] == "x"
    assert series["y"]["matcher"]["options"] == "y"
    assert panel["options"]["mapping"] == "manual"


def test_a_radar_bar_is_grouped_so_its_scores_are_not_summed() -> None:
    radar = {
        "kind": "radar",
        "title": "governorate profile across 4 metrics",
        "series": ["Muscat", "Dhofar"],
        "data": [
            {"name": "revenue_omr_000", "Muscat": 100, "Dhofar": 37.4},
            {"name": "arpu_omr", "Muscat": 94.6, "Dhofar": 100},
        ],
    }
    panel = build_dashboard_with_data("R", [radar], testdata_uid="t")["panels"][0]
    assert panel["type"] == "barchart"
    assert panel["options"]["stacking"] == "none", "0-100 scores share no unit"
    assert panel["options"]["legend"]["showLegend"] is True


def test_kpi_tiles_do_not_crowd_out_the_charts(charts: list[dict]) -> None:
    """Four stat tiles counted against a six-chart budget left room for two
    plots, and the governorate bar fell off the dashboard entirely."""
    stats = [c for c in charts if c["kind"] == "stat"]
    plots = [c for c in charts if c["kind"] != "stat"]
    assert len(stats) == 4
    assert len(plots) >= 6, [c["title"] for c in plots]
    titles = " | ".join(c["title"] for c in plots)
    assert "governorate" in titles, "the category breakdown survived the cap"
