"""DuckDB analysis engine — full-file profiling & aggregation, zero truncation.

Phase 0 of the Analysis Engine: chat attachments stay on the proven stdlib
profiler (small, already whole files), but UPLOADED DATASETS (the new big-file
path) are profiled and charted HERE — every aggregate runs as SQL over the
ENTIRE file, and the model only ever sees the resulting ground-truth numbers.
Output shape is exactly `build_data_dashboard`'s spec (stats / line / bar /
pie / table with recipe keys), so all three renderers and the edit semantics
work unchanged.

Security: `path` must live under the datasets directory (checked by the
caller against the DB row the user OWNS); column identifiers come from the
file's own header and are always double-quote escaped before entering SQL.

Complexity: one SQL pass per chart; DuckDB streams from disk — RAM stays flat
regardless of file size.
"""

from __future__ import annotations

from typing import Any

from sila.services.sila.data_dashboard import _numeric_id_like

_MAX_CATEGORIES = 12
_MAX_LINE_POINTS = 300
_MAX_STATS = 4
_MAX_CHARTS = 6
_TABLE_ROWS = 15

_NUMBER_TYPES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "FLOAT",
    "DOUBLE",
    "DECIMAL",
)
_DATE_TYPES = ("DATE", "TIMESTAMP", "TIMESTAMP_NS", "TIMESTAMP WITH TIME ZONE")


def _q(identifier: str) -> str:
    """Quote a column identifier from an untrusted CSV header."""
    return '"' + identifier.replace('"', '""') + '"'


def _connect(path: str):  # noqa: ANN202 — duckdb type only exists at runtime
    import duckdb

    conn = duckdb.connect()
    # CREATE VIEW can't be a prepared statement — escape the path as a SQL
    # string literal (it's OUR uuid-named storage path, escaped regardless).
    lit = path.replace("'", "''")
    conn.execute(f"CREATE VIEW dataset AS SELECT * FROM read_csv_auto('{lit}', sample_size=-1)")
    return conn


def profile_dataset(path: str) -> dict[str, Any]:
    """{rows, columns:[{name, kind}]} over the FULL file. kind mirrors the
    stdlib profiler: number / date / category / id / text."""
    conn = _connect(path)
    try:
        rows = int(conn.execute("SELECT count(*) FROM dataset").fetchone()[0])
        described = conn.execute("DESCRIBE dataset").fetchall()
        columns: list[dict[str, Any]] = []
        for name, dtype, *_ in described:
            base = str(dtype).upper()
            if any(base.startswith(t) for t in _NUMBER_TYPES):
                kind = "text" if _numeric_id_like(str(name), []) else "number"
            elif any(base.startswith(t) for t in _DATE_TYPES):
                kind = "date"
            else:
                distinct = int(
                    conn.execute(f"SELECT count(DISTINCT {_q(str(name))}) FROM dataset").fetchone()[
                        0
                    ]
                )
                if distinct <= 24:
                    kind = "category"
                elif rows and distinct / rows >= 0.8:
                    kind = "id"
                else:
                    kind = "text"
            columns.append({"name": str(name), "kind": kind})
        return {"rows": rows, "columns": columns}
    finally:
        conn.close()


def _delta_sql(conn: Any, column: str, date_col: str) -> float | None:
    """Percent change of the recent half against the earlier half, in SQL.

    Mirrors `data_dashboard._direction` exactly — the two paths must agree or a
    KPI changes colour depending on whether the data arrived as an attachment or
    as an uploaded dataset, and a parity test enforces that.
    """
    row = conn.execute(
        f"WITH ordered AS ("
        f"  SELECT {_q(column)} AS v, row_number() OVER (ORDER BY {_q(date_col)}) AS rn,"
        f"         count(*) OVER () AS total"
        f"  FROM dataset WHERE {_q(column)} IS NOT NULL AND {_q(date_col)} IS NOT NULL)"
        f" SELECT avg(v) FILTER (WHERE rn <= total / 2) AS earlier,"
        f"        avg(v) FILTER (WHERE rn > total / 2) AS recent,"
        f"        max(total) FROM ordered"
    ).fetchone()
    if row is None:
        return None
    earlier, recent, total = row
    if earlier is None or recent is None or not total or int(total) < 4 or float(earlier) == 0:
        return None
    return round((float(recent) - float(earlier)) / abs(float(earlier)) * 100, 1)


def _stats_sql(
    conn: Any, num_cols: list[str], rows: int, date_col: str | None = None
) -> list[dict[str, Any]]:
    charts = []
    for c in num_cols[:_MAX_STATS]:
        avg, lo, hi, n = conn.execute(
            f"SELECT avg({_q(c)}), min({_q(c)}), max({_q(c)}), count({_q(c)}) FROM dataset"
        ).fetchone()
        if avg is None:
            continue
        stat: dict[str, Any] = {
            "kind": "stat",
            "title": f"{c} (avg)",
            "value": round(float(avg), 2),
            "sub": f"min {round(float(lo), 2):g} · max {round(float(hi), 2):g} · {int(n)} rows",
            "metric": c,
            "agg": "avg",
        }
        if date_col:
            change = _delta_sql(conn, c, date_col)
            if change is not None:
                stat["delta_pct"] = change
        charts.append(stat)
    return charts


def _line_charts_sql(conn: Any, date_col: str, num_cols: list[str]) -> list[dict[str, Any]]:
    """One or more line charts, mirroring the stdlib `_line_charts`.

    A metric more than _AXIS_RATIO below the largest gets its own chart: one
    axis cannot show subscribers near 400,000 and revenue near 2,700 at once —
    the larger flattens the smaller onto the baseline.
    """
    from sila.services.sila.data_dashboard import _AXIS_RATIO, _MAX_TRENDS

    series = num_cols[:3]
    if not series:
        return []
    points = _line_points_sql(conn, date_col, series)
    if not points:
        return []
    scales = {
        name: max((abs(p[name]) for p in points if name in p), default=0.0) for name in series
    }
    biggest = max(scales.values(), default=0.0)
    together = [c for c in series if biggest and scales[c] * _AXIS_RATIO >= biggest]
    apart = [c for c in series if c not in together]
    # Same trend cap as the stdlib path, or the two diverge and the parity test
    # (rightly) fails.
    groups = (([together] if together else []) + [[c] for c in apart])[:_MAX_TRENDS]
    return [
        _line_chart_from(
            date_col, group, [{"x": p["x"], **{n: p[n] for n in group if n in p}} for p in points]
        )
        for group in groups
    ]


def _line_points_sql(conn: Any, date_col: str, series: list[str]) -> list[dict[str, Any]]:
    sel = ", ".join(f"avg({_q(c)}) AS {_q(c)}" for c in series)
    rows = conn.execute(
        f"SELECT date_trunc('day', {_q(date_col)}) AS d, {sel} FROM dataset "
        f"WHERE {_q(date_col)} IS NOT NULL GROUP BY 1 ORDER BY 1"
    ).fetchall()
    points = [
        {
            "x": str(r[0])[:10],
            **{c: round(float(v), 2) for c, v in zip(series, r[1:], strict=False) if v is not None},
        }
        for r in rows
    ]
    if len(points) > _MAX_LINE_POINTS:
        step = len(points) / _MAX_LINE_POINTS
        points = [points[int(i * step)] for i in range(_MAX_LINE_POINTS)]
    return points


def _line_chart_from(
    date_col: str, series: list[str], points: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "kind": "line",
        "title": f"{' & '.join(series)} over {date_col}",
        "series": series,
        "data": points,
        "by": date_col,
        "series_cols": series,
    }


def _bar_sql(conn: Any, cat_col: str, num_col: str) -> dict[str, Any]:
    rows = conn.execute(
        f"SELECT {_q(cat_col)}, sum({_q(num_col)}) AS v FROM dataset "
        f"WHERE {_q(cat_col)} IS NOT NULL GROUP BY 1 ORDER BY v DESC"
    ).fetchall()
    head, tail = rows[:_MAX_CATEGORIES], rows[_MAX_CATEGORIES:]
    data = [{"name": str(k), "value": round(float(v or 0), 2)} for k, v in head]
    if tail:
        data.append({"name": "Other", "value": round(sum(float(v or 0) for _, v in tail), 2)})
    return {
        "kind": "bar",
        "title": f"{num_col} by {cat_col}",
        "data": data,
        "metric": num_col,
        "by": cat_col,
        "agg": "sum",
    }


def _pie_sql(conn: Any, cat_col: str) -> dict[str, Any]:
    rows = conn.execute(
        f"SELECT {_q(cat_col)}, count(*) AS v FROM dataset "
        f"WHERE {_q(cat_col)} IS NOT NULL GROUP BY 1 ORDER BY v DESC"
    ).fetchall()
    head, tail = rows[:_MAX_CATEGORIES], rows[_MAX_CATEGORIES:]
    data = [{"name": str(k), "value": int(v)} for k, v in head]
    if tail:
        data.append({"name": "Other", "value": int(sum(v for _, v in tail))})
    return {"kind": "pie", "title": f"Breakdown by {cat_col}", "data": data, "by": cat_col}


def _table_sql(conn: Any, cols: list[str], sort_metric: str | None) -> dict[str, Any]:
    sel = ", ".join(_q(c) for c in cols)
    order = f"ORDER BY {_q(sort_metric)} DESC" if sort_metric else ""
    rows = conn.execute(f"SELECT {sel} FROM dataset {order} LIMIT {_TABLE_ROWS}").fetchall()
    out = [
        {
            c: (
                round(float(v), 4)
                if isinstance(v, int | float) and not isinstance(v, bool)
                else str(v)
            )
            for c, v in zip(cols, r, strict=False)
        }
        for r in rows
    ]
    return {
        "kind": "table",
        "title": f"Top rows by {sort_metric}" if sort_metric else "Data sample",
        "columns": cols,
        "data": out,
        "metric": sort_metric,
    }


def dashboard_from_dataset(
    path: str,
    filename: str,
    focus: str | None = None,
    extra_charts: list[dict[str, Any]] | None = None,
    provenance: Any | None = None,
) -> dict[str, Any]:
    """The full-file twin of `build_data_dashboard` — identical output shape,
    every number computed by DuckDB over ALL rows. Raises ValueError on an
    unusable file (same contract)."""
    try:
        prof = profile_dataset(path)
    except Exception as exc:  # noqa: BLE001 — surface as the standard contract
        raise ValueError(f"couldn't read the dataset: {exc}") from exc
    rows, columns = prof["rows"], prof["columns"]
    if rows == 0:
        raise ValueError("The file needs a header row and at least one data row.")

    focus_l = (focus or "").strip().lower()

    def focused(names: list[str]) -> list[str]:
        return sorted(names, key=lambda n: 0 if focus_l and focus_l in n.lower() else 1)

    num_cols = focused([c["name"] for c in columns if c["kind"] == "number"])
    date_cols = [c["name"] for c in columns if c["kind"] == "date"]
    cat_cols = focused([c["name"] for c in columns if c["kind"] == "category"])
    if not num_cols and not cat_cols:
        raise ValueError(
            "No chartable columns found — the file needs at least one numeric or category column."
        )

    conn = _connect(path)
    try:
        charts: list[dict[str, Any]] = _stats_sql(
            conn, num_cols, rows, date_cols[0] if date_cols else None
        )
        if date_cols and num_cols:
            charts.extend(_line_charts_sql(conn, date_cols[0], num_cols))
        for cat in cat_cols:
            if num_cols:
                charts.append(_bar_sql(conn, cat, num_cols[0]))
        if cat_cols and len(charts) < _MAX_CHARTS:
            charts.append(_pie_sql(conn, cat_cols[0]))
        charts = charts[:_MAX_CHARTS]
        # P5 chart-chooser grammar — the SAME suggestions as the stdlib path,
        # so both build routes earn identical visuals for the same shape.
        from sila.services.analysis.chooser import suggest_extras

        for suggestion in suggest_extras(columns, rows, len(charts)):
            built = _custom_sql(conn, columns, suggestion)
            if built is not None:
                charts.append(built)
        table_cols = ([*cat_cols[:2], *num_cols[:4]] or [c["name"] for c in columns])[:6]
        if table_cols:
            charts.append(_table_sql(conn, table_cols, num_cols[0] if num_cols else None))
        for spec in extra_charts or []:
            built = _custom_sql(conn, columns, spec)
            if built is not None:
                charts.append(built)
    finally:
        conn.close()

    if provenance is not None:
        for c in charts:
            provenance.record(
                artifact=str(c.get("title", "")),
                source=filename,
                operations=[f"duckdb:{c.get('agg', c.get('kind', ''))} over {rows} rows"],
            )
    return {
        "title": filename.rsplit(".", 1)[0].replace("_", " ").title(),
        "source": filename,
        "rows": rows,
        "columns": columns,
        "charts": charts,
        "notes": [],
    }


def _custom_sql(
    conn: Any, columns: list[dict[str, Any]], spec: dict[str, Any]
) -> dict[str, Any] | None:
    """A model-described chart ({kind, metric, by, agg, title}) computed in SQL —
    the dataset twin of data_dashboard._custom_chart."""
    if not isinstance(spec, dict):
        return None
    names = {c["name"].lower(): c["name"] for c in columns}

    def find(n: Any) -> str | None:
        key = str(n or "").strip().lower()
        return names.get(key) or next((v for k, v in names.items() if key and key in k), None)

    kind = str(spec.get("kind", "bar")).lower()
    how = str(spec.get("agg", "avg")).lower()
    how = how if how in ("avg", "sum", "min", "max", "count") else "avg"
    metric, by = find(spec.get("metric")), find(spec.get("by"))
    title = str(spec.get("title", "") or "").strip()[:120]

    if kind == "stat" and metric:
        val = conn.execute(f"SELECT {how}({_q(metric)}) FROM dataset").fetchone()[0]
        if val is None:
            return None
        return {
            "kind": "stat",
            "title": title or f"{metric} ({how})",
            "value": round(float(val), 2),
            "sub": "full file",
            "metric": metric,
            "agg": how,
        }
    if kind == "line" and metric:
        date = next((c["name"] for c in columns if c["kind"] == "date"), None)
        if date:
            built = _line_charts_sql(conn, date, [metric])
            if not built:
                return None
            chart = built[0]
            return {**chart, "title": title or chart["title"]}
        return None
    if kind == "pie" and by:
        if metric:
            rows = conn.execute(
                f"SELECT {_q(by)}, sum({_q(metric)}) AS v FROM dataset "
                f"WHERE {_q(by)} IS NOT NULL GROUP BY 1 ORDER BY v DESC LIMIT 11"
            ).fetchall()
            data = [{"name": str(k), "value": round(float(v or 0), 2)} for k, v in rows]
            return {
                "kind": "pie",
                "title": title or f"{metric} share by {by}",
                "data": data,
                "metric": metric,
                "by": by,
            }
        chart = _pie_sql(conn, by)
        return {**chart, "title": title or chart["title"]}
    if kind == "table":
        wanted = [w for n in (spec.get("columns") or []) if (w := find(n))]
        cols = wanted or [c["name"] for c in columns][:6]
        return _table_sql(conn, cols[:6], metric)
    kinds = {c["name"]: c["kind"] for c in columns}
    if kind == "scatter" and metric:
        x_name = (
            by
            if by and kinds.get(by) == "number" and by != metric
            else next(
                (c["name"] for c in columns if c["kind"] == "number" and c["name"] != metric), None
            )
        )
        if not x_name:
            return None
        rows_xy = conn.execute(
            f"SELECT {_q(x_name)}, {_q(metric)} FROM dataset "
            f"WHERE {_q(x_name)} IS NOT NULL AND {_q(metric)} IS NOT NULL LIMIT 500"
        ).fetchall()
        data = [{"x": round(float(x), 4), "y": round(float(y), 4)} for x, y in rows_xy]
        if len(data) < 3:
            return None
        return {
            "kind": "scatter",
            "title": title or f"{metric} vs {x_name}",
            "data": data,
            "metric": metric,
            "by": x_name,
        }

    if kind == "histogram" and metric:
        lo, hi = conn.execute(
            f"SELECT min({_q(metric)}), max({_q(metric)}) FROM dataset"
        ).fetchone()
        if lo is None or hi is None or float(hi) <= float(lo):
            return None
        lo, width = float(lo), (float(hi) - float(lo)) / 12
        buckets = conn.execute(
            f"SELECT least(11, greatest(0, CAST(floor(({_q(metric)} - {lo!r}) / {width!r}) "
            f"AS INTEGER))) AS b, count(*) FROM dataset WHERE {_q(metric)} IS NOT NULL GROUP BY 1"
        ).fetchall()
        if sum(int(n) for _, n in buckets) < 5:
            return None
        counts = [0] * 12
        for b, n in buckets:
            counts[int(b)] = int(n)
        data = [
            {"name": f"{lo + i * width:g}–{lo + (i + 1) * width:g}", "value": c}
            for i, c in enumerate(counts)
        ]
        return {
            "kind": "histogram",
            "title": title or f"Distribution of {metric}",
            "data": data,
            "metric": metric,
            "agg": "count",
        }

    if kind == "radar" and by and kinds.get(by) == "category":
        wanted = [w for n in (spec.get("columns") or []) if (w := find(n))]
        mnames = [
            m
            for m in (wanted or [c["name"] for c in columns if c["kind"] == "number"][:6])
            if kinds.get(m) == "number"
        ]
        if len(mnames) < 3:
            return None
        groups = [
            str(g)
            for g, _ in conn.execute(
                f"SELECT CAST({_q(by)} AS VARCHAR), count(*) FROM dataset "
                f"WHERE {_q(by)} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 5"
            ).fetchall()
        ]
        if len(groups) < 2:
            return None
        sel = ", ".join(f"avg({_q(m)})" for m in mnames)
        mean_rows = conn.execute(
            f"SELECT CAST({_q(by)} AS VARCHAR), {sel} FROM dataset "
            f"WHERE {_q(by)} IS NOT NULL GROUP BY 1"
        ).fetchall()
        means = {
            (str(r[0]), m): float(v)
            for r in mean_rows
            for m, v in zip(mnames, r[1:], strict=False)
            if v is not None
        }
        data = []
        for m in mnames:
            peak = max((means.get((g, m), 0.0) for g in groups), default=0.0)
            row: dict[str, Any] = {"name": m}
            for g in groups:
                row[g] = round(means.get((g, m), 0.0) / peak * 100, 1) if peak else 0.0
            data.append(row)
        return {
            "kind": "radar",
            "title": title or f"{by} profile across {len(mnames)} metrics",
            "data": data,
            "series": groups,
            "by": by,
            "columns": mnames,
        }

    if metric:  # bar default
        axis = by or next((c["name"] for c in columns if c["kind"] == "category"), None)
        if axis:
            chart = _bar_sql(conn, axis, metric)
            return {**chart, "title": title or chart["title"]}
    return None


def export_csv_sample(path: str, limit: int = 50_000) -> tuple[str, bool]:
    """(csv_text, sampled) for renderers that need raw rows (the Power BI push).
    Row-capped honestly — `sampled` tells the caller to say so."""
    conn = _connect(path)
    try:
        total = int(conn.execute("SELECT count(*) FROM dataset").fetchone()[0])
        rel = conn.execute(f"SELECT * FROM dataset LIMIT {int(limit)}")
        cols = [d[0] for d in rel.description]
        import csv as _csv
        import io as _io

        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(cols)
        for row in rel.fetchall():
            w.writerow(["" if v is None else v for v in row])
        return buf.getvalue(), total > limit
    finally:
        conn.close()
