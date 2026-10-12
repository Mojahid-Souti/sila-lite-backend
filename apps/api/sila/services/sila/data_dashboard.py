"""CSV → an in-Sila dashboard spec — deterministic profiling, stdlib only.

The user drops a CSV in the chat and asks for a dashboard; this module parses it,
profiles the columns (number / date / category), and picks the charts a sensible
analyst would: time + numbers → line; category + number → bar; a lone
low-cardinality category → pie; headline numbers → stat cards. The output is a
plain chart spec the frontend renders with Recharts — the data never leaves the
box (the sovereign answer to "upload a file, get a dashboard").

The MODEL never touches the rows: it only names an optional focus column. All
parsing/aggregation is code — a 7B is never trusted with arithmetic.

Complexity: O(rows × cols) single pass per chart; caps bound every output.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_MAX_ROWS = 5_000  # rows considered (attachment cap keeps real inputs far smaller)
_MAX_LINE_POINTS = 300  # points per line chart (downsampled evenly past this)
_MAX_CATEGORIES = 12  # bars/slices shown (the rest grouped into "Other")
_MAX_CHARTS = 6
_MAX_STATS = 4
_CATEGORY_MAX_UNIQUE = 24  # a text column with more distinct values isn't a category
_TITLE_MAX = 120

_DATE_PATTERNS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%b %d, %Y",
    "%d %b %Y",
)

_NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?%?$")


@dataclass
class Column:
    name: str
    kind: str  # "number" | "date" | "category" | "text"
    values: list[Any] = field(default_factory=list)  # parsed (numbers/dates) or raw


def _parse_number(raw: str) -> float | None:
    s = raw.strip().replace(",", "").rstrip("%")
    if not s or not _NUM_RE.match(raw.strip()):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(raw: str) -> datetime | None:
    s = raw.strip()
    if not s:
        return None
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_csv(text: str, max_rows: int = _MAX_ROWS) -> tuple[list[str], list[list[str]]]:
    """Header + rows. Sniffs the delimiter (comma fallback). Raises ValueError on
    anything that isn't at least a header plus one data row. `max_rows` lets the
    Power BI push keep EVERY row it was given — the default 5k cap made Power BI
    aggregate a different sample than the chat dashboard (live parity bug)."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    table = [row for row in reader if any(cell.strip() for cell in row)]
    if len(table) < 2:
        raise ValueError("The file needs a header row and at least one data row.")
    header = [h.strip() or f"col{i + 1}" for i, h in enumerate(table[0])]
    width = len(header)
    rows = [(r + [""] * width)[:width] for r in table[1 : max_rows + 1]]
    return header, rows


def _numeric_id_like(name: str, nums: list[float | None]) -> bool:
    """A KEY masquerading as a number (order_id, id): averaging it is meaningless
    and it feeds the model a nonsense stat to narrate ("order_id (avg) 100.51K").
    NAME-based only — a value heuristic (unique consecutive ints) was tried and
    reverted: it also matches legitimate unique integer METRICS. O(1)."""
    del nums  # signature kept for a future, smarter value check
    n = name.strip().lower()
    return n == "id" or n.endswith("_id") or n.endswith(" id") or n.endswith("-id")


def profile_columns(header: list[str], rows: list[list[str]]) -> list[Column]:
    """Type-infer each column by parse-success ratio over its non-empty cells."""
    cols: list[Column] = []
    for i, name in enumerate(header):
        cells = [r[i].strip() for r in rows]
        non_empty = [c for c in cells if c]
        if not non_empty:
            cols.append(Column(name=name, kind="text", values=cells))
            continue
        nums = [_parse_number(c) for c in non_empty]
        if sum(v is not None for v in nums) / len(non_empty) >= 0.8:
            if _numeric_id_like(name, nums):
                # Keys stay out of charts/stats entirely (still shown in tables).
                cols.append(Column(name=name, kind="text", values=cells))
            else:
                cols.append(
                    Column(name=name, kind="number", values=[_parse_number(c) for c in cells])
                )
            continue
        dates = [_parse_date(c) for c in non_empty]
        if sum(v is not None for v in dates) / len(non_empty) >= 0.8:
            cols.append(Column(name=name, kind="date", values=[_parse_date(c) for c in cells]))
            continue
        uniques = {c.lower() for c in non_empty}
        if len(uniques) <= _CATEGORY_MAX_UNIQUE:
            kind = "category"
        elif len(uniques) / len(non_empty) >= 0.8:
            kind = "id"  # unique-ish label per row (symbol, name, sku) → top-N bars
        else:
            kind = "text"
        cols.append(Column(name=name, kind=kind, values=cells))
    return cols


def _downsample(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(points) <= _MAX_LINE_POINTS:
        return points
    step = len(points) / _MAX_LINE_POINTS
    return [points[int(i * step)] for i in range(_MAX_LINE_POINTS)]


# Two metrics on one axis are only comparable within about this factor. Beyond
# it the larger one flattens the smaller to the baseline: subscribers near
# 400,000 rendered revenue near 2,700 as a straight line along zero.
_AXIS_RATIO = 20.0
# How many time-series panels a dashboard carries before it reads as repetition.
_MAX_TRENDS = 2


def _aggregate_by_date(
    date_col: Column, num_cols: list[Column]
) -> list[tuple[Any, dict[str, float]]]:
    """One point per DATE, whatever the row count — the MEAN of that date's rows.

    The file is a panel: 12 months x 4 governorates x 2 segments = 96 rows, so
    every month appears eight times with values from 606 to 3,743. Plotted in
    date order without aggregating, that is a sawtooth between the smallest and
    largest region — which is exactly what shipped.

    The mean, not the sum, because the DuckDB engine's `_line_sql` has always
    used `avg()` and a parity test holds the two paths to the same answer. One
    rule beats a per-metric guess at which column is additive. O(rows x metrics).
    """
    buckets: dict[Any, dict[str, list[float]]] = {}
    for i, when in enumerate(date_col.values):
        if when is None:
            continue
        bucket = buckets.setdefault(when, {})
        for col in num_cols:
            value = col.values[i] if i < len(col.values) else None
            if isinstance(value, float):
                bucket.setdefault(col.name, []).append(value)
    out: list[tuple[Any, dict[str, float]]] = []
    for when in sorted(buckets):
        point: dict[str, float] = {}
        for col in num_cols:
            values = buckets[when].get(col.name)
            if not values:
                continue
            point[col.name] = round(sum(values) / len(values), 2)
        out.append((when, point))
    return out


def _line_charts(date_col: Column, num_cols: list[Column]) -> list[dict[str, Any]]:
    """One or more line charts over `date_col`, aggregated per date.

    Metrics that share an axis honestly travel together; a metric more than
    _AXIS_RATIO away from the largest gets its own chart, because one axis
    cannot show both.
    """
    wanted = num_cols[:3]
    if not wanted:
        return []
    pairs = _aggregate_by_date(date_col, wanted)
    if not pairs:
        return []

    def scale(name: str) -> float:
        values = [abs(vals[name]) for _, vals in pairs if name in vals]
        return max(values) if values else 0.0

    scales = {c.name: scale(c.name) for c in wanted}
    biggest = max(scales.values(), default=0.0)
    together = [c for c in wanted if biggest and scales[c.name] * _AXIS_RATIO >= biggest]
    apart = [c for c in wanted if c not in together]

    def build(group: list[Column]) -> dict[str, Any]:
        names = [c.name for c in group]
        points = [
            {"x": when.strftime("%Y-%m-%d"), **{n: vals[n] for n in names if n in vals}}
            for when, vals in pairs
        ]
        return {
            "kind": "line",
            "title": f"{' & '.join(names)} over {date_col.name}",
            "series": names,
            "data": _downsample(points),
            # Recipe (for the Power BI mirror): axis + metric column names.
            "by": date_col.name,
            "series_cols": names,
        }

    charts = [build(together)] if together else []
    charts += [build([c]) for c in apart]
    # Three lines of the same shape read as one chart repeated — a reviewer's
    # words: "a lot of line chart… not that nice". Two trends earn their place;
    # past that the extra metric is better served by the category breakdowns and
    # the KPI tiles, which already carry its level and its direction.
    return charts[:_MAX_TRENDS]


def _line_chart(date_col: Column, num_cols: list[Column]) -> dict[str, Any]:
    """The first line chart only — for callers that want exactly one."""
    charts = _line_charts(date_col, num_cols)
    return charts[0] if charts else {"kind": "line", "title": "", "series": [], "data": []}


def _bar_chart(cat_col: Column, num_col: Column) -> dict[str, Any]:
    sums: dict[str, float] = defaultdict(float)
    for i, cat in enumerate(cat_col.values):
        label = str(cat).strip()
        v = num_col.values[i] if i < len(num_col.values) else None
        if label and isinstance(v, float):
            sums[label] += v
    top = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)
    head, tail = top[:_MAX_CATEGORIES], top[_MAX_CATEGORIES:]
    data = [{"name": k, "value": round(v, 2)} for k, v in head]
    if tail:
        data.append({"name": "Other", "value": round(sum(v for _, v in tail), 2)})
    return {
        "kind": "bar",
        "title": f"{num_col.name} by {cat_col.name}",
        "data": data,
        "metric": num_col.name,
        "by": cat_col.name,
        "agg": "sum",
    }


def _top_n_bar(id_col: Column, num_col: Column) -> dict[str, Any]:
    """Top-N rows by a metric, labelled by the id column ("Top EPS by Stock")."""
    pairs = [
        (str(id_col.values[i]).strip(), num_col.values[i])
        for i in range(min(len(id_col.values), len(num_col.values)))
        if str(id_col.values[i]).strip() and isinstance(num_col.values[i], float)
    ]
    top = sorted(pairs, key=lambda kv: kv[1], reverse=True)[:_MAX_CATEGORIES]
    return {
        "kind": "bar",
        "title": f"Top {len(top)} {num_col.name} by {id_col.name}",
        "data": [{"name": k, "value": round(v, 2)} for k, v in top],
        "metric": num_col.name,
        "by": id_col.name,
    }


def _pie_chart(cat_col: Column) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    for cat in cat_col.values:
        label = str(cat).strip()
        if label:
            counts[label] += 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    head, tail = top[:_MAX_CATEGORIES], top[_MAX_CATEGORIES:]
    data = [{"name": k, "value": v} for k, v in head]
    if tail:
        data.append({"name": "Other", "value": sum(v for _, v in tail)})
    return {
        "kind": "pie",
        "title": f"Breakdown by {cat_col.name}",
        "data": data,
        "by": cat_col.name,
    }


def _direction(values: list[float]) -> float | None:
    """Percent change of the RECENT half against the earlier half.

    A KPI with no comparison is just a number. Splitting the (date-ordered)
    series in two and comparing the means is the honest comparison available
    without calendar logic: it needs no fixed period, works on any row count,
    and degrades to None when there is nothing to compare.

    Returns None when the series is too short or the earlier half averages
    zero (a percent change against zero is not a number anyone can act on).
    """
    if len(values) < 4:
        return None
    half = len(values) // 2
    earlier, recent = values[:half], values[half:]
    base = sum(earlier) / len(earlier)
    if base == 0:
        return None
    return (sum(recent) / len(recent) - base) / abs(base) * 100


def _stats(num_cols: list[Column], date_col: Column | None = None) -> list[dict[str, Any]]:
    """Headline = the AVERAGE (a sum of ratio columns like ROE is meaningless);
    min/max ride the sub-line so outliers are visible at a glance. When the data
    is dated, each stat also carries `delta_pct` — recent half against earlier —
    so the number says whether it is moving, not just where it sits."""
    out = []
    for c in num_cols[:_MAX_STATS]:
        vals = [v for v in c.values if isinstance(v, float)]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        stat: dict[str, Any] = {
            "kind": "stat",
            "title": f"{c.name} (avg)",
            "value": round(avg, 2),
            "sub": f"min {round(min(vals), 2):g} · max {round(max(vals), 2):g} · {len(vals)} rows",
            "metric": c.name,
            "agg": "avg",
        }
        if date_col is not None:
            # Order by date before splitting, or "recent" means "further down
            # the file", which is not the same thing.
            paired = [
                (d, v)
                for d, v in zip(date_col.values, c.values, strict=False)
                if d is not None and isinstance(v, float)
            ]
            ordered = [v for _, v in sorted(paired, key=lambda pair: str(pair[0]))]
            change = _direction(ordered)
            if change is not None:
                stat["delta_pct"] = round(change, 1)
        out.append(stat)
    return out


def _data_notes(cols: list[Column]) -> list[str]:
    """Analyst-grade observations computed HERE (never by the model): outlier-
    skewed metrics where averages mislead, near-duplicate columns, 0/1 flags.
    The assistant weaves these into its dashboard introduction. O(n·cols)."""
    notes: list[str] = []
    for c in cols:
        if c.kind != "number" or len(notes) >= 2:
            continue
        vals = sorted(v for v in c.values if isinstance(v, float))
        if len(vals) < 8:
            continue
        median = vals[len(vals) // 2]
        spread = max(abs(vals[0]), abs(vals[-1]))
        if spread > 50 * max(abs(median), 1e-9) and spread > 100:
            notes.append(
                f"'{c.name}' has extreme outliers (min {vals[0]:g}, max {vals[-1]:g}, "
                f"median {median:g}) — averages mislead here, medians are safer"
            )
    seen: dict[str, str] = {}
    for c in cols:
        key = re.sub(r"[^a-z0-9]", "", c.name.lower())
        if key in seen and len(notes) < 4:
            notes.append(f"'{seen[key]}' and '{c.name}' look like near-duplicate columns")
        seen.setdefault(key, c.name)
    for c in cols:
        if c.kind == "number" and len(notes) < 5:
            uniq = {v for v in c.values if isinstance(v, float)}
            if uniq and uniq <= {0.0, 1.0}:
                ones = sum(1 for v in c.values if v == 1.0)
                zeros = sum(1 for v in c.values if v == 0.0)
                notes.append(
                    f"'{c.name}' is a 0/1 label ({ones} ones / {zeros} zeros) — "
                    "treated as a class flag, not a metric"
                )
    return notes[:4]


def _is_index_column(col: Column) -> bool:
    """An UNNAMED numeric column (header cell was empty → auto-named colN) is a
    spreadsheet export's row index/artifact, not data — charting it is noise.
    (A sequence check missed gappy indexes live: "col1 · min 0 · max 81".)"""
    return col.kind == "number" and bool(re.fullmatch(r"col\d+", col.name))


def _find_col(cols: list[Column], name: str) -> Column | None:
    """Fuzzy column lookup (case-insensitive; containment fallback)."""
    n = (name or "").strip().lower()
    if not n:
        return None
    for c in cols:
        if c.name.lower() == n:
            return c
    for c in cols:
        if n in c.name.lower() or c.name.lower() in n:
            return c
    return None


def _agg(vals: list[float], how: str) -> float:
    if not vals:
        return 0.0
    if how == "sum":
        return sum(vals)
    if how == "min":
        return min(vals)
    if how == "max":
        return max(vals)
    if how == "count":
        return float(len(vals))
    return sum(vals) / len(vals)  # avg — the default


def _custom_chart(cols: list[Column], spec: dict[str, Any]) -> dict[str, Any] | None:
    """One MODEL-DESCRIBED chart ({kind, metric, by, agg, title}) computed from the
    real columns. The model only names things — every number comes from code.
    Returns None when the spec doesn't match the data."""
    if not isinstance(spec, dict):
        return None
    kind = str(spec.get("kind", "bar")).lower()
    how = str(spec.get("agg", "avg")).lower()
    metric = _find_col(cols, str(spec.get("metric", "")))
    by = _find_col(cols, str(spec.get("by", "")))
    title = str(spec.get("title", "") or "").strip()[:_TITLE_MAX]

    if kind == "stat" and metric is not None:
        vals = [v for v in metric.values if isinstance(v, float)]
        return {
            "kind": "stat",
            "title": title or f"{metric.name} ({how})",
            "value": round(_agg(vals, how), 2),
            "sub": f"{len(vals)} rows",
            "metric": metric.name,
            "agg": how,
        }

    if kind == "line" and metric is not None:
        # A dated file charts over time; otherwise the metric across rows in file
        # order (the "trend" of a keyed dataset), labelled by `by` when given.
        date_col = next((c for c in cols if c.kind == "date"), None)
        if date_col is not None:
            return {**_line_chart(date_col, [metric]), "title": title or f"{metric.name} trend"}
        labels = by.values if by is not None else list(range(1, len(metric.values) + 1))
        if by is not None and by.kind == "category":
            # A category axis has duplicate labels — one point per RAW row drew
            # "West West West…" junk (live bug). Aggregate: avg per label, in
            # first-appearance order.
            sums: dict[str, list[float]] = {}
            ordered: list[str] = []
            for i, label in enumerate(labels):
                key = str(label).strip()
                v = metric.values[i] if i < len(metric.values) else None
                if key and isinstance(v, float):
                    if key not in sums:
                        ordered.append(key)
                        sums[key] = []
                    sums[key].append(v)
            data = [{"x": k, metric.name: round(sum(sums[k]) / len(sums[k]), 2)} for k in ordered]
        else:
            data = [
                {"x": str(labels[i]), metric.name: metric.values[i]}
                for i in range(len(metric.values))
                if isinstance(metric.values[i], float)
            ]
        return {
            "kind": "line",
            "title": title or f"{metric.name} trend",
            "series": [metric.name],
            "data": _downsample(data),
            "by": by.name if by is not None else None,
            "series_cols": [metric.name],
        }

    if kind == "pie" and by is not None:
        if metric is not None:
            # Share of a metric by label — "Market Cap share by stock".
            sums: dict[str, float] = defaultdict(float)
            for i, label in enumerate(by.values):
                key = str(label).strip()
                v = metric.values[i] if i < len(metric.values) else None
                if key and isinstance(v, float) and v > 0:
                    sums[key] += v
            top = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)
            head, tail = top[:10], top[10:]
            data = [{"name": k, "value": round(v, 2)} for k, v in head]
            if tail:
                data.append({"name": "Other", "value": round(sum(v for _, v in tail), 2)})
            return {
                "kind": "pie",
                "title": title or f"{metric.name} share by {by.name}",
                "data": data,
                "metric": metric.name,
                "by": by.name,
            }
        chart = _pie_chart(by)
        return {**chart, "title": title or chart["title"]}

    if kind == "table":
        wanted = spec.get("columns")
        table_cols = (
            [c for n in wanted if (c := _find_col(cols, str(n))) is not None]
            if isinstance(wanted, list) and wanted
            else cols[:6]
        )
        if not table_cols:
            return None
        limit = min(int(spec.get("limit", 12) or 12), 50)
        order = list(range(len(table_cols[0].values)))
        if metric is not None:
            order.sort(
                key=lambda i: (
                    metric.values[i] if isinstance(metric.values[i], float) else float("-inf")
                ),
                reverse=True,
            )
        rows_out = []
        for i in order[:limit]:
            row = {}
            for c in table_cols:
                v = c.values[i] if i < len(c.values) else ""
                row[c.name] = round(v, 4) if isinstance(v, float) else str(v)
            rows_out.append(row)
        return {
            "kind": "table",
            "title": title
            or ("Top rows by " + metric.name if metric is not None else "Data sample"),
            "columns": [c.name for c in table_cols],
            "data": rows_out,
            "metric": metric.name if metric is not None else None,
        }

    if kind == "scatter" and metric is not None:
        x_col = (
            by
            if by is not None and by.kind == "number" and by.name != metric.name
            else next((c for c in cols if c.kind == "number" and c.name != metric.name), None)
        )
        if x_col is None:
            return None
        data = [
            {"x": round(x, 4), "y": round(y, 4)}
            for x, y in zip(x_col.values, metric.values, strict=False)
            if isinstance(x, float) and isinstance(y, float)
        ][:500]
        if len(data) < 3:
            return None
        return {
            "kind": "scatter",
            "title": title or f"{metric.name} vs {x_col.name}",
            "data": data,
            "metric": metric.name,
            "by": x_col.name,
        }

    if kind == "histogram" and metric is not None:
        vals = sorted(v for v in metric.values if isinstance(v, float))
        if len(vals) < 5 or vals[-1] <= vals[0]:
            return None
        lo, width = vals[0], (vals[-1] - vals[0]) / 12
        counts = [0] * 12
        for v in vals:
            counts[min(11, int((v - lo) / width))] += 1
        data = [
            {"name": f"{lo + i * width:g}–{lo + (i + 1) * width:g}", "value": c}
            for i, c in enumerate(counts)
        ]
        return {
            "kind": "histogram",
            "title": title or f"Distribution of {metric.name}",
            "data": data,
            "metric": metric.name,
            "agg": "count",
        }

    if kind == "radar" and by is not None and by.kind == "category":
        wanted_metrics = spec.get("columns") or [c.name for c in cols if c.kind == "number"][:6]
        mcols = [
            c
            for n in wanted_metrics
            if (c := _find_col(cols, str(n))) is not None and c.kind == "number"
        ]
        if len(mcols) < 3:
            return None
        counts: dict[str, int] = defaultdict(int)
        sums: dict[tuple[str, str], float] = defaultdict(float)
        nums: dict[tuple[str, str], int] = defaultdict(int)
        for i, label in enumerate(by.values):
            group = str(label).strip()
            if not group:
                continue
            counts[group] += 1
            for m in mcols:
                v = m.values[i] if i < len(m.values) else None
                if isinstance(v, float):
                    sums[(group, m.name)] += v
                    nums[(group, m.name)] += 1
        groups = [g for g, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
        if len(groups) < 2:
            return None
        means = {
            (g, m.name): sums[(g, m.name)] / nums[(g, m.name)]
            for g in groups
            for m in mcols
            if nums[(g, m.name)]
        }
        data = []
        for m in mcols:
            peak = max((means.get((g, m.name), 0.0) for g in groups), default=0.0)
            row: dict[str, Any] = {"name": m.name}
            for g in groups:
                row[g] = round(means.get((g, m.name), 0.0) / peak * 100, 1) if peak else 0.0
            data.append(row)
        return {
            "kind": "radar",
            "title": title or f"{by.name} profile across {len(mcols)} metrics",
            "data": data,
            "series": groups,
            "by": by.name,
            "columns": [m.name for m in mcols],
        }

    if metric is None:
        return None
    # bar (default): grouped when `by` is category-ish; top-N when it's an id.
    if by is not None and by.kind == "id":
        chart = _top_n_bar(by, metric)
    elif by is not None:
        chart = _bar_chart(by, metric)
    else:
        id_col = next((c for c in cols if c.kind == "id"), None)
        if id_col is None:
            return None
        chart = _top_n_bar(id_col, metric)
    return {**chart, "title": title or chart["title"]}


# A single row cannot be charted into anything: no trend, no comparison, no
# share. Two is left alone deliberately — several fixtures and one real
# behaviour (a series too short to compare) are specified at two rows.
_MIN_DASHBOARD_ROWS = 2


def _plots(charts: list[dict[str, Any]]) -> int:
    """Charts that occupy a plot slot. A stat is a KPI tile, laid out four
    across in its own row — counting tiles against the chart budget is what let
    four figures push the governorate bar off the dashboard entirely."""
    return sum(1 for c in charts if c.get("kind") != "stat")


def _cap_plots(charts: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keep every stat, and the first `limit` plots, in order."""
    out: list[dict[str, Any]] = []
    seen = 0
    for chart in charts:
        if chart.get("kind") == "stat":
            out.append(chart)
            continue
        if seen < limit:
            out.append(chart)
            seen += 1
    return out


def build_data_dashboard(
    filename: str,
    text: str,
    focus: str | None = None,
    extra_charts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The full pipeline: parse → profile → pick charts. `focus` (an optional
    column name the user mentioned) promotes that column's charts first;
    `extra_charts` are MODEL-DESCRIBED charts ({kind, metric, by, agg, title})
    computed here deterministically and appended — the chat-editing path.
    Returns {title, source, charts:[…]}; raises ValueError on unusable input."""
    header, rows = parse_csv(text)
    cols = profile_columns(header, rows)
    cols = [c for c in cols if not _is_index_column(c)]
    date_cols = [c for c in cols if c.kind == "date"]
    num_cols = [c for c in cols if c.kind == "number"]
    cat_cols = [c for c in cols if c.kind == "category"]
    id_cols = [c for c in cols if c.kind == "id"]

    focus_l = (focus or "").strip().lower()

    def focused_first(items: list[Column]) -> list[Column]:
        if not focus_l:
            return items
        return sorted(items, key=lambda c: 0 if focus_l in c.name.lower() else 1)

    num_cols = focused_first(num_cols)
    cat_cols = focused_first(cat_cols)

    charts: list[dict[str, Any]] = list(_stats(num_cols, date_cols[0] if date_cols else None))
    if date_cols and num_cols:
        charts.extend(_line_charts(date_cols[0], num_cols))
    # Keyed data (stock symbols, names): top-N bars for the leading REAL metrics
    # (near-binary flags like a 0/1 Class column rank meaninglessly).
    if id_cols:
        varied = [c for c in num_cols if len({v for v in c.values if isinstance(v, float)}) > 4]
        for num in varied[:2]:
            charts.append(_top_n_bar(id_cols[0], num))
    for cat in cat_cols:
        for num in num_cols:
            charts.append(_bar_chart(cat, num))
            break  # one bar per category column (against its top number)
    if cat_cols and not num_cols or cat_cols and _plots(charts) < _MAX_CHARTS:
        charts.append(_pie_chart(cat_cols[0]))

    charts = _cap_plots(charts, _MAX_CHARTS)
    # P5 chart-chooser grammar: extra visual kinds this data shape earns
    # (radar / scatter / histogram) — suggested by score, validated by the
    # builder, shared with the DuckDB path so both pick identically.
    from sila.services.analysis.chooser import suggest_extras

    for suggestion in suggest_extras(
        [{"name": c.name, "kind": c.kind} for c in cols], len(rows), len(charts)
    ):
        built = _custom_chart(cols, suggestion)
        if built is not None:
            charts.append(built)
    extra_specs = list(extra_charts or [])
    # A searchable data table is part of every default dashboard (top rows by
    # the leading metric) — unless the user already asked for their own table.
    has_table = any(str(s.get("kind")) == "table" for s in extra_specs if isinstance(s, dict))
    if not has_table and (id_cols or cat_cols) and num_cols:
        default_table = _custom_chart(
            cols, {"kind": "table", "metric": num_cols[0].name, "limit": 15}
        )
        if default_table is not None:
            charts.append(default_table)
    for spec in extra_specs:
        built = _custom_chart(cols, spec)
        if built is not None:
            charts.append(built)
    charts = _cap_plots(charts, _MAX_CHARTS + 5)
    if not charts:
        raise ValueError(
            "No chartable columns found — the file needs at least one numeric, "
            "date, or category column."
        )
    # A dashboard of one row is not a dashboard. A mailbox export with a single
    # message produced a "Breakdown by from" pie of one sender address, which
    # looks like a broken feature rather than an empty dataset.
    if len(rows) < _MIN_DASHBOARD_ROWS:
        raise ValueError(
            f"only {len(rows)} row — there is nothing to chart. State the figure "
            "in text instead, or chart a file that has more rows."
        )
    title = re.sub(r"\.(csv|txt)$", "", filename, flags=re.IGNORECASE).replace("_", " ").strip()
    return {
        "title": (title or "Data dashboard").title(),
        "source": filename,
        "rows": len(rows),
        "columns": [{"name": c.name, "kind": c.kind} for c in cols],
        "charts": charts,
        # Deterministic analyst observations for the assistant's introduction.
        "notes": _data_notes(cols),
    }
