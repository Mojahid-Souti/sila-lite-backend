"""Build a Grafana dashboard JSON model from a plain spec — stdlib only.

Grafana's dashboard is a documented JSON schema (rows/panels with a grid layout),
so "create a Grafana dashboard" is generation, the same shape as building a .docx:
turn a small spec into the JSON Grafana's `POST /api/dashboards/db` accepts. No
Grafana SDK — the stack is frozen; it's just a dict.

The spec is deliberately tiny (title + a list of panels, each a type + a query),
so a model or a form can produce it. We lay panels out on Grafana's 24-column
grid automatically (two per row), pick sane defaults, and never trust the caller
for anything structural.

Complexity: O(panels) — one pass to place + serialize.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

# Panel types we support → Grafana panel `type`. Anything else falls back to a
# timeseries (the safe, universally-rendered default).
_PANEL_TYPES = {
    "timeseries": "timeseries",
    "graph": "timeseries",
    "line": "timeseries",
    "stat": "stat",
    "number": "stat",
    "gauge": "gauge",
    "bar": "barchart",
    "barchart": "barchart",
    "table": "table",
    "text": "text",
    "piechart": "piechart",
    "pie": "piechart",
}

_GRID_W = 24  # Grafana's fixed column count
_PANEL_W = 12  # two panels per row
_PANEL_H = 8

_MAX_PANELS = 24  # a dashboard spec can't be unbounded
_TITLE_MAX = 120
_QUERY_MAX = 2000


def _panel(
    panel_id: int,
    spec: dict[str, Any],
    x: int,
    y: int,
    datasource_uid: str | None,
    datasource_type: str | None = None,
) -> dict[str, Any]:
    """One panel dict placed at (x, y). `spec` = {title, type, query?, unit?, text?}."""
    ptype = _PANEL_TYPES.get(str(spec.get("type", "")).lower(), "timeseries")
    title = str(spec.get("title", "") or "").strip()[:_TITLE_MAX] or f"Panel {panel_id}"
    panel: dict[str, Any] = {
        "id": panel_id,
        "type": ptype,
        "title": title,
        "gridPos": {"h": _PANEL_H, "w": _PANEL_W, "x": x, "y": y},
    }

    if ptype == "text":
        # A markdown/text panel carries content, not a query.
        panel["options"] = {"mode": "markdown", "content": str(spec.get("text", ""))[:_QUERY_MAX]}
        return panel

    # A data panel: a single target. The query language is the datasource's
    # (PromQL/SQL/…); we pass it through verbatim — Grafana validates it.
    query = str(spec.get("query", "") or "").strip()[:_QUERY_MAX]
    ds = (
        {"type": datasource_type or "datasource", "uid": datasource_uid} if datasource_uid else None
    )
    target: dict[str, Any] = {"refId": "A", "expr": query, "rawSql": query}
    if ds:
        target["datasource"] = ds
    panel["targets"] = [target]
    if ds:
        panel["datasource"] = ds

    unit = str(spec.get("unit", "") or "").strip()
    if unit:
        panel["fieldConfig"] = {"defaults": {"unit": unit[:32]}, "overrides": []}
    return panel


def build_dashboard(
    title: str,
    panels: list[dict[str, Any]],
    *,
    datasource_uid: str | None = None,
    datasource_type: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Spec → the JSON body for `POST /api/dashboards/db` (wrap as {"dashboard":…,
    "overwrite": false} at the call site). Panels are auto-placed two per row.

    Raises ValueError on an empty title or no panels — never emits a broken board."""
    clean_title = (title or "").strip()[:_TITLE_MAX]
    if not clean_title:
        raise ValueError("The dashboard needs a title.")
    if not panels:
        raise ValueError("The dashboard needs at least one panel.")

    out: list[dict[str, Any]] = []
    for i, spec in enumerate(panels[:_MAX_PANELS]):
        if not isinstance(spec, dict):
            continue
        col = i % 2
        row = i // 2
        out.append(
            _panel(
                panel_id=i + 1,
                spec=spec,
                x=col * _PANEL_W,
                y=row * _PANEL_H,
                datasource_uid=datasource_uid,
                datasource_type=datasource_type,
            )
        )
    if not out:
        raise ValueError("No valid panels in the spec.")

    return {
        "uid": None,  # Grafana assigns one
        "title": clean_title,
        "tags": [str(t)[:32] for t in (tags or ["sila"])][:10],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 0,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": out,
    }


# ---------------------------------------------------------------------------
# Data-carrying dashboards — the uploaded file's numbers INSIDE the dashboard.
# Grafana's built-in TestData plugin (`grafana-testdata-datasource`) renders a
# `csv_content` query, so each panel can embed its own rows: real charts on the
# user's Grafana with NO metrics pipeline. Static by design (re-run to refresh).
# ---------------------------------------------------------------------------

_TESTDATA_TYPE = "grafana-testdata-datasource"


def _csv_escape(v: Any) -> str:
    s = str(v)
    return f'"{s.replace(chr(34), chr(34) * 2)}"' if ("," in s or '"' in s) else s


_DATE_X = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _chart_csv(chart: dict[str, Any]) -> str:
    """One chart spec (from data_dashboard.build_data_dashboard) → inline CSV."""
    kind = chart.get("kind")
    if kind == "stat":
        return f"value\n{chart.get('value', 0)}"
    if kind == "table":
        cols = [str(c) for c in chart.get("columns", [])]
        lines = [",".join(_csv_escape(c) for c in cols)]
        for row in chart.get("data", []):
            lines.append(",".join(_csv_escape(row.get(c, "")) for c in cols))
        return "\n".join(lines)
    if kind == "line":
        series = [str(s) for s in chart.get("series", [])]
        data = chart.get("data", [])
        dated = bool(data) and bool(_DATE_X.match(str(data[0].get("x", ""))))
        if dated:
            lines = ["time," + ",".join(_csv_escape(s) for s in series)]
            for pt in data:
                # RFC3339 so the TestData CSV reader types the column as time.
                lines.append(
                    f"{pt.get('x', '')}T00:00:00Z," + ",".join(str(pt.get(s, "")) for s in series)
                )
            return "\n".join(lines)
        # No dates (row-order "trend"): numeric x — rendered by a `trend` panel
        # (a timeseries panel would error "Data is missing a time field").
        lines = ["x," + ",".join(_csv_escape(s) for s in series)]
        for i, pt in enumerate(data):
            lines.append(f"{i + 1}," + ",".join(str(pt.get(s, "")) for s in series))
        return "\n".join(lines)
    if kind == "scatter":
        lines = ["x,y"]
        for pt in chart.get("data", []):
            lines.append(f"{pt.get('x', '')},{pt.get('y', '')}")
        return "\n".join(lines)
    if kind == "radar":
        # No core radar panel — a grouped bar per metric keeps the comparison.
        series = [str(s) for s in chart.get("series", [])]
        lines = ["name," + ",".join(_csv_escape(s) for s in series)]
        for pt in chart.get("data", []):
            lines.append(
                f"{_csv_escape(pt.get('name', ''))}," + ",".join(str(pt.get(s, "")) for s in series)
            )
        return "\n".join(lines)
    # bar / pie / histogram: name,value rows
    lines = ["name,value"]
    for pt in chart.get("data", []):
        lines.append(f"{_csv_escape(pt.get('name', ''))},{pt.get('value', 0)}")
    return "\n".join(lines)


_DATA_PANEL_TYPE = {
    "line": "timeseries",
    "bar": "barchart",
    "pie": "piechart",
    "stat": "stat",
    # P5 kinds — faithful core-panel fallbacks (no plugin dependencies).
    "histogram": "barchart",
    "scatter": "xychart",
    "radar": "barchart",
}


def _panel_type_for(chart: dict[str, Any]) -> str:
    """The Grafana panel type for a chart — a dateless line uses `trend` (numeric
    x axis; a timeseries panel would demand a time field), the rest map directly."""
    kind = str(chart.get("kind", ""))
    if kind == "line":
        data = chart.get("data", [])
        dated = bool(data) and bool(_DATE_X.match(str(data[0].get("x", ""))))
        return "timeseries" if dated else "trend"
    return _DATA_PANEL_TYPE.get(kind, "table")


# A KPI row: four across, short. The old layout gave every stat half the width
# and most of the height, so four figures pushed every chart below the fold.
_STAT_H = 4
_STAT_MIN_W = 4

# Past this a category name cannot sit under a vertical bar without colliding
# with its neighbours.
_LONG_CATEGORY_CHARS = 14

# Distinct, calm accents for KPIs that have no direction to show. Green and red
# are deliberately absent: they mean "rising" and "falling" on this dashboard,
# and a decorative colour must not be mistaken for a verdict.
_KPI_ACCENTS = ("blue", "purple", "orange", "#8AB8FF", "#B877D9", "#FFB357")


def _series_count(chart: dict[str, Any]) -> int:
    """How many numeric series a chart carries — a point with several numeric
    keys beyond its label is a multi-series (grouped) chart."""
    data = chart.get("data") or []
    if not data or not isinstance(data[0], dict):
        return 1
    return max(1, sum(1 for k, v in data[0].items() if k not in ("name", "x") and _numeric(v)))


def _numeric(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _has_long_categories(chart: dict[str, Any]) -> bool:
    """Would this chart's category names collide under vertical bars?

    Categorical points carry `name`; a dated/numeric series carries `x`. Both are
    checked so the rule does not silently miss one shape.
    """
    return any(
        len(str(point.get("name", point.get("x", "")))) > _LONG_CATEGORY_CHARS
        for point in chart.get("data", [])
        if isinstance(point, dict)
    )


def _row_widths(count: int) -> list[int]:
    """Widths that sum to EXACTLY 24 for `count` panels on one row.

    An exact fill is the whole point. A row left short — five stats at width 6
    leaves 18 columns empty — is not merely ugly: Grafana compacts panels
    upward into free space, so the next chart floats up beside the stray stat
    and the dashboard reads as if panels landed at random. Remainder columns go
    to the leftmost panels, so the row is flush on both edges.
    """
    base, spare = divmod(_GRID_W, max(1, count))
    return [base + (1 if i < spare else 0) for i in range(count)]


def _rows_of(indexes: list[int], per_row: int) -> list[list[int]]:
    return [indexes[i : i + per_row] for i in range(0, len(indexes), per_row)]


def _layout(charts: list[dict[str, Any]]) -> list[dict[str, int]]:
    """Place panels on Grafana's 24-column grid.

    Every stat sits in a KPI strip at the top, then the charts flow two per row.
    Each row fills the full 24 columns, so Grafana never floats a panel up into
    a gap. O(panels).
    """
    stats = [i for i, c in enumerate(charts) if _panel_type_for(c) == "stat"]
    rest = [i for i in range(len(charts)) if i not in set(stats)]
    boxes: list[dict[str, int]] = [{} for _ in charts]

    # Up to four KPIs share a row; five or more split evenly rather than
    # stranding one on a row of its own.
    per_row = len(stats) if len(stats) <= 4 else (len(stats) + 1) // 2
    y = 0
    for row in _rows_of(stats, max(1, per_row)):
        x = 0
        for index, width in zip(row, _row_widths(len(row)), strict=True):
            boxes[index] = {"h": _STAT_H, "w": width, "x": x, "y": y}
            x += width
        y += _STAT_H

    for row in _rows_of(rest, 2):
        x = 0
        for index, width in zip(row, _row_widths(len(row)), strict=True):
            boxes[index] = {"h": _PANEL_H, "w": width, "x": x, "y": y}
            x += width
        y += _PANEL_H
    return boxes


def build_dashboard_with_data(
    title: str, charts: list[dict[str, Any]], testdata_uid: str
) -> dict[str, Any]:
    """Chart specs (the in-Sila profiler's output) → a Grafana dashboard whose
    panels EMBED their data as TestData csv_content queries. Raises ValueError on
    an empty title/charts — same contract as build_dashboard."""
    clean_title = (title or "").strip()[:_TITLE_MAX]
    if not clean_title:
        raise ValueError("The dashboard needs a title.")
    if not charts:
        raise ValueError("The dashboard needs at least one chart.")
    ds = {"type": _TESTDATA_TYPE, "uid": testdata_uid}
    panels: list[dict[str, Any]] = []
    usable = [c for c in charts[:_MAX_PANELS] if isinstance(c, dict)]
    placement = _layout(usable)
    for i, (chart, box) in enumerate(zip(usable, placement, strict=True)):
        ptype = _panel_type_for(chart)
        panel: dict[str, Any] = {
            "id": i + 1,
            "type": ptype,
            "title": str(chart.get("title", "") or f"Panel {i + 1}")[:_TITLE_MAX],
            "gridPos": box,
            "datasource": ds,
            "targets": [
                {
                    "refId": "A",
                    "datasource": ds,
                    "scenarioId": "csv_content",
                    "csvContent": _chart_csv(chart),
                }
            ],
        }
        if ptype == "stat":
            # A KPI with no comparison is just a number, so colour carries the
            # DIRECTION of travel. Up is green and down is red — the convention —
            # but that only means "good" where up is good, so a cost or discount
            # metric will need inverting once we know which those are. Grafana's
            # own default thresholds are still off: they painted "89.8" red for
            # sitting under a number nobody chose.
            delta = chart.get("delta_pct")
            if isinstance(delta, int | float) and delta:
                colour = "green" if delta > 0 else "red"
                arrow = "▲" if delta > 0 else "▼"
                panel["title"] = f"{panel['title']}  {arrow} {abs(delta):g}%"
            else:
                # No comparison to draw on — undated data, or too few rows. A
                # KPI strip in one flat grey still reads as unfinished, so each
                # figure takes its own accent from a fixed rotation. That is
                # decoration and says nothing about the number, which is exactly
                # why it must not borrow green or red.
                colour = _KPI_ACCENTS[len(panels) % len(_KPI_ACCENTS)]
            panel["options"] = {
                "colorMode": "value",
                "graphMode": "none",
                "textMode": "auto",
                "justifyMode": "auto",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            }
            panel["fieldConfig"] = {
                "defaults": {
                    "color": {"mode": "fixed", "fixedColor": colour},
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [{"color": colour, "value": None}],
                    },
                },
                "overrides": [],
            }
        elif ptype == "barchart":
            # Long category names on a vertical axis overlap into an unreadable
            # smear (live: the objective names ran together). Horizontal bars
            # give each name its own row.
            horizontal = _has_long_categories(chart)
            # Stack where stacking MEANS something: a multi-series breakdown
            # shows its total and its composition at once. A single series has
            # nothing to stack, so it stays a plain bar — which also gives the
            # dashboard two bar shapes instead of the same one repeated.
            series = _series_count(chart)
            # A radar's series are each metric SCALED to 0-100 on its own terms,
            # so they share no unit. Stacked, they summed into "100 / 91.7 / 400"
            # rows that mean nothing. Grouped, the comparison the radar was for
            # survives.
            stacked = series > 1 and str(chart.get("kind")) != "radar"
            panel["options"] = {
                "orientation": "horizontal" if horizontal else "auto",
                "xTickLabelRotation": 0 if horizontal else -45,
                "xTickLabelMaxLength": 0 if horizontal else 18,
                "stacking": "normal" if stacked else "none",
                "showValue": "auto",
                "legend": {
                    "showLegend": series > 1,
                    "displayMode": "list",
                    "placement": "bottom",
                },
            }
        elif ptype == "xychart":
            # Grafana 11's xychart will not initialise without a series mapping
            # — the panel sits on "Loading plugin panel…" forever, which is what
            # shipped. `x`/`y` are the column names _chart_csv writes.
            panel["options"] = {
                "mapping": "manual",
                "series": [
                    {
                        "x": {"matcher": {"id": "byName", "options": "x"}},
                        "y": {"matcher": {"id": "byName", "options": "y"}},
                    }
                ],
                "legend": {"showLegend": False},
                "tooltip": {"mode": "single", "sort": "none"},
            }
            # setdefault: a panel built without a fieldConfig must not KeyError.
            defaults = panel.setdefault("fieldConfig", {}).setdefault("defaults", {})
            defaults["custom"] = {
                "show": "points",
                "pointSize": {"fixed": 5},
                "axisPlacement": "auto",
            }
        elif ptype == "piechart":
            # Without values:true the panel REDUCES all rows into one "value"
            # series — a single solid circle (live bug). values:true slices per
            # row, labelled by the frame's string column.
            panel["options"] = {
                "pieType": "donut",
                "reduceOptions": {"values": True, "calcs": ["lastNotNull"], "fields": ""},
                "legend": {"displayMode": "list", "placement": "right"},
            }
        panels.append(panel)
    if not panels:
        raise ValueError("No valid charts.")
    return {
        "uid": None,
        "title": clean_title,
        "tags": ["sila", "sila-data"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 0,
        # Fit the time window to the DATA's span — the old now-5y default
        # squeezed an 18-month series into a sliver of the panel.
        "time": _time_window(charts),
        "panels": panels,
    }


def _time_window(charts: list[dict[str, Any]]) -> dict[str, str]:
    """{from,to} covering every line chart's x range (±1 day margin); the now-5y
    default only when no dated series exists. O(points)."""
    xs: list[str] = []
    for chart in charts:
        if not isinstance(chart, dict) or chart.get("kind") != "line":
            continue
        for point in chart.get("data") or []:
            x = str(point.get("x", "")) if isinstance(point, dict) else ""
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
                xs.append(x)
    if not xs:
        return {"from": "now-5y", "to": "now"}
    lo, hi = min(xs), max(xs)
    lo_dt = datetime.strptime(lo, "%Y-%m-%d") - timedelta(days=1)
    hi_dt = datetime.strptime(hi, "%Y-%m-%d") + timedelta(days=1)
    return {
        "from": lo_dt.strftime("%Y-%m-%dT00:00:00Z"),
        "to": hi_dt.strftime("%Y-%m-%dT23:59:59Z"),
    }
