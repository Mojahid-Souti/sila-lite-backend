"""Hand-built Power BI "thin report" (.pbix) — chat → real visuals, no clicks.

A PBIX is a zip. A THIN report (live connection to an existing dataset) needs no
data model inside — just a Connections part pointing at the dataset and a
Report/Layout part describing pages + visuals. We build both from the same
column profile that drives the in-Sila/Grafana dashboards, then the service
imports the file via Power BI's documented Imports API.

⚠ REVERSE-ENGINEERED, demo-grade: the Layout JSON schema is Power BI Desktop's
internal format (stable for years — pbi-tools et al. rely on it — but NOT a
Microsoft contract). If Microsoft changes it, imports fail; the service catches
that and falls back to the dataset link with a "connector issue, we're on it"
message — the data push itself never depends on this module.

Encodings matter: Layout/Metadata/Settings/Version are UTF-16LE; Connections
and [Content_Types].xml are UTF-8. Getting these wrong = "corrupt file" on
import.

Complexity: O(visuals) — a handful of dict builds + one zip write.
"""

from __future__ import annotations

import json
import re
import secrets
from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from sila.services.sila.data_dashboard import Column

_TABLE = "Data"  # the push dataset's single table (powerbi_service._TABLE)
_PAGE_W, _PAGE_H = 1280, 720
_AVG, _SUM = 1, 0  # prototypeQuery aggregation Function enum
_DESC = 2  # OrderBy Direction enum (2 = descending)
_MAX_CARDS = 4
_MAX_BARS = 2
_TABLE_COLS = 6

# Auto-named blank headers ("col3") — never worth a visual.
_JUNK_NAME_RE = re.compile(r"^(col\d+|unnamed.*)$", re.IGNORECASE)


def _is_junk_metric(col: Column) -> bool:
    """Columns that make meaningless visuals: auto-named blanks, and 0/1 class
    flags (a bar of 'Class by symbol' is a wall of identical bars — the live
    failure on the financial CSV)."""
    if _JUNK_NAME_RE.match(col.name.strip()):
        return True
    if col.kind == "number":
        values = {v for v in col.values if isinstance(v, float)}
        if values and values <= {0.0, 1.0}:
            return True
    return False


def _vname() -> str:
    return secrets.token_hex(10)


def _col_expr(prop: str) -> dict[str, Any]:
    return {"Column": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": prop}}


def _agg_expr(prop: str, fn: int) -> dict[str, Any]:
    return {"Aggregation": {"Expression": _col_expr(prop), "Function": fn}}


def _agg_ref(prop: str, fn: int) -> str:
    name = {_SUM: "Sum", _AVG: "Avg"}[fn]
    return f"{name}({_TABLE}.{prop})"


def _query(select: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "Version": 2,
        "From": [{"Name": "d", "Entity": _TABLE, "Type": 0}],
        "Select": select,
    }


def _lit(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def _pt(size: int) -> dict[str, Any]:
    """A font-size property literal (PBI encodes points as '<n>D')."""
    return _lit(f"{size}D")


# Readable text everywhere — the imported report's defaults were tiny (live
# complaint). Per-visual `objects` (axes/legend/labels/table text); unknown
# properties are ignored by Power BI, so these degrade safely.
_AXIS_OBJECTS: dict[str, Any] = {
    "categoryAxis": [{"properties": {"fontSize": _pt(12)}}],
    "valueAxis": [{"properties": {"fontSize": _pt(12)}}],
}
_PIE_OBJECTS: dict[str, Any] = {
    "legend": [{"properties": {"fontSize": _pt(12)}}],
    "labels": [{"properties": {"fontSize": _pt(11)}}],
}
_TABLE_OBJECTS: dict[str, Any] = {
    "values": [{"properties": {"fontSize": _pt(12)}}],
    "columnHeaders": [{"properties": {"fontSize": _pt(12), "bold": _lit("true")}}],
}


def _visual(
    kind: str,
    x: int,
    y: int,
    w: int,
    h: int,
    projections: dict[str, list[dict[str, Any]]],
    select: list[dict[str, Any]],
    title: str | None = None,
    objects: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One visualContainer. The inner config rides as STRINGIFIED json — that's
    the format, not an accident."""
    config: dict[str, Any] = {
        "name": _vname(),
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 0, "width": w, "height": h}}],
        "singleVisual": {
            "visualType": kind,
            "projections": projections,
            "prototypeQuery": _query(select),
            "drillFilterOtherVisuals": True,
        },
    }
    if objects:
        config["singleVisual"]["objects"] = objects
    if title:
        config["singleVisual"]["vcObjects"] = {
            "title": [
                {
                    "properties": {
                        "text": _lit(f"'{title}'"),
                        "show": _lit("true"),
                        "fontSize": _pt(13),
                        "bold": _lit("true"),
                    }
                }
            ]
        }
    return {
        "x": x,
        "y": y,
        "z": 0,
        "width": w,
        "height": h,
        "config": json.dumps(config, ensure_ascii=False),
    }


def _card(col: str, x: int, y: int, w: int, h: int) -> dict[str, Any]:
    ref = _agg_ref(col, _AVG)
    return _visual(
        "card",
        x,
        y,
        w,
        h,
        {"Values": [{"queryRef": ref}]},
        [{**_agg_expr(col, _AVG), "Name": ref, "NativeReferenceName": f"Avg of {col}"}],
        title=f"{col} (avg)",
    )


def _bar(label: str, metric: str, x: int, y: int, w: int, h: int) -> dict[str, Any]:
    metric_ref = _agg_ref(metric, _AVG)
    visual = _visual(
        "clusteredBarChart",
        x,
        y,
        w,
        h,
        {
            "Category": [{"queryRef": f"{_TABLE}.{label}", "active": True}],
            "Y": [{"queryRef": metric_ref}],
        },
        [
            {**_col_expr(label), "Name": f"{_TABLE}.{label}", "NativeReferenceName": label},
            {**_agg_expr(metric, _AVG), "Name": metric_ref, "NativeReferenceName": metric},
        ],
        title=f"{metric} by {label}",
        objects=_AXIS_OBJECTS,
    )
    # Sort by the metric descending — top performers first, not alphabetical
    # (589 alphabetical bars was the live "very bad" complaint).
    config = json.loads(visual["config"])
    config["singleVisual"]["prototypeQuery"]["OrderBy"] = [
        {"Direction": _DESC, "Expression": _agg_expr(metric, _AVG)}
    ]
    visual["config"] = json.dumps(config, ensure_ascii=False)
    return visual


def _table_visual(columns: list[str], x: int, y: int, w: int, h: int) -> dict[str, Any]:
    """A row-level data table (tableEx) over the key columns."""
    return _visual(
        "tableEx",
        x,
        y,
        w,
        h,
        {"Values": [{"queryRef": f"{_TABLE}.{c}"} for c in columns]},
        [{**_col_expr(c), "Name": f"{_TABLE}.{c}", "NativeReferenceName": c} for c in columns],
        objects=_TABLE_OBJECTS,
    )


def _line(date_col: str, metric: str, x: int, y: int, w: int, h: int) -> dict[str, Any]:
    metric_ref = _agg_ref(metric, _AVG)
    return _visual(
        "lineChart",
        x,
        y,
        w,
        h,
        {
            "Category": [{"queryRef": f"{_TABLE}.{date_col}", "active": True}],
            "Y": [{"queryRef": metric_ref}],
        },
        [
            {
                **_col_expr(date_col),
                "Name": f"{_TABLE}.{date_col}",
                "NativeReferenceName": date_col,
            },
            {**_agg_expr(metric, _AVG), "Name": metric_ref, "NativeReferenceName": metric},
        ],
        title=f"{metric} over {date_col}",
        objects=_AXIS_OBJECTS,
    )


def _pie(label: str, metric: str, x: int, y: int, w: int, h: int) -> dict[str, Any]:
    metric_ref = _agg_ref(metric, _SUM)
    return _visual(
        "pieChart",
        x,
        y,
        w,
        h,
        {
            "Category": [{"queryRef": f"{_TABLE}.{label}", "active": True}],
            "Y": [{"queryRef": metric_ref}],
        },
        [
            {**_col_expr(label), "Name": f"{_TABLE}.{label}", "NativeReferenceName": label},
            {**_agg_expr(metric, _SUM), "Name": metric_ref, "NativeReferenceName": metric},
        ],
        title=f"{metric} share by {label}",
        objects=_PIE_OBJECTS,
    )


def _fuzzy(cols: list[Column], name: str) -> str | None:
    """Case-insensitive column lookup with a containment fallback."""
    n = (name or "").strip().lower()
    if not n:
        return None
    for c in cols:
        if c.name.lower() == n:
            return c.name
    for c in cols:
        if n in c.name.lower() or c.name.lower() in n:
            return c.name
    return None


def pick_visuals(
    cols: list[Column],
    extra_charts: list[dict[str, Any]] | None = None,
    *,
    authoritative: bool = False,
) -> list[dict[str, Any]]:
    """The same instincts as the in-Sila dashboard, expressed as LIVE visuals —
    PLUS the user's own accumulated chart specs (the exact list the in-chat
    dashboard renders), so chat and Power BI stay in step: stat cards, DESC
    bars by the best label, a line over dates, pies, and row-level tables.
    Junk metrics (auto-named blanks, 0/1 flags) are excluded everywhere.
    Rows flow down the page; the section height grows to fit.

    `authoritative=True` (the chat path): `extra_charts` is the FULL recipe
    list the in-chat dashboard rendered — build EXACTLY those visuals and skip
    the auto-derivation, so chat, Grafana, and Power BI mirror each other."""
    numbers = [c.name for c in cols if c.kind == "number" and not _is_junk_metric(c)]
    dates = [c.name for c in cols if c.kind == "date"]
    categories = [c.name for c in cols if c.kind == "category"]
    ids = [c.name for c in cols if c.kind == "id"]
    label = (ids + categories)[0] if (ids or categories) else None

    cards: list[str] = []
    slots: list[tuple[str, str, str]] = []  # (kind, axis column, metric)
    tables: list[list[str]] = []
    if not authoritative:
        cards = list(numbers[:_MAX_CARDS])
        if label:
            slots.extend(("bar", label, m) for m in numbers[:_MAX_BARS])
        if dates and numbers:
            slots.append(("line", dates[0], numbers[0]))
        if label and numbers and categories:
            slots.append(("pie", categories[0], numbers[0]))
        slots = slots[:3]

    # The user's requested charts, mapped onto the same visual builders.
    for spec in extra_charts or []:
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("kind", "bar")).lower()
        if kind in ("scatter", "histogram", "radar"):
            # Derived/normalized views (bins, 0–100 scaling) don't exist in the
            # pushed raw table — mirrored in chat/Grafana, skipped here.
            continue
        metric = _fuzzy(cols, str(spec.get("metric") or ""))
        by = _fuzzy(cols, str(spec.get("by") or ""))
        if kind == "stat":
            if metric and metric not in cards:
                cards.append(metric)
        elif kind == "table":
            wanted = [w for n in (spec.get("columns") or []) if (w := _fuzzy(cols, str(n)))]
            tables.append(wanted[:_TABLE_COLS])
        elif kind == "pie":
            axis = by or (categories[0] if categories else label)
            # A count-pie ("Breakdown by segment") has no metric — approximate
            # with the leading number so the mirror keeps the slice.
            pie_metric = metric or (numbers[0] if numbers else None)
            if axis and pie_metric:
                slots.append(("pie", axis, pie_metric))
        elif kind == "line":
            axis = by if by in dates else (dates[0] if dates else None)
            # The chat line chart carries several series — mirror up to two.
            series_cols = [
                m for n in (spec.get("series_cols") or []) if (m := _fuzzy(cols, str(n)))
            ]
            for m in series_cols[:2] or ([metric] if metric else []):
                if axis and m:
                    slots.append(("line", axis, m))
        elif metric:  # bar (default)
            axis = by or label
            if axis:
                slots.append(("bar", axis, metric))

    default_table = (([label] if label else []) + numbers)[:_TABLE_COLS]
    tables = [t or default_table for t in tables] or ([default_table] if default_table else [])

    visuals: list[dict[str, Any]] = []
    y = 8
    cards = cards[:6]
    if cards:
        card_w = _PAGE_W // len(cards)
        for i, col in enumerate(cards):
            visuals.append(_card(col, x=i * card_w + 8, y=y, w=card_w - 16, h=132))
        y += 144
    for start in range(0, min(len(slots), 6), 3):
        row = slots[start : start + 3]
        w_mid = _PAGE_W // len(row)
        for i, (kind, axis, metric) in enumerate(row):
            x = i * w_mid + 8
            if kind == "bar":
                visuals.append(_bar(axis, metric, x, y, w_mid - 16, 300))
            elif kind == "line":
                visuals.append(_line(axis, metric, x, y, w_mid - 16, 300))
            else:
                visuals.append(_pie(axis, metric, x, y, w_mid - 16, 300))
        y += 312
    for table_cols in tables[:2]:
        if table_cols:
            visuals.append(_table_visual(table_cols, x=8, y=y, w=_PAGE_W - 16, h=224))
            y += 236
    return visuals


def _layout(title: str, visuals: list[dict[str, Any]]) -> bytes:
    # The page grows vertically to fit every visual (the report scrolls).
    page_h = max(_PAGE_H, max((int(v["y"]) + int(v["height"]) for v in visuals), default=0) + 8)
    layout = {
        "id": 0,
        "sections": [
            {
                "id": 0,
                "name": "ReportSection1",
                "displayName": title[:60] or "Dashboard",
                "visualContainers": visuals,
                "filters": "[]",
                "config": "{}",
                "displayOption": 1,
                "width": _PAGE_W,
                "height": page_h,
            }
        ],
        "filters": "[]",
        "config": json.dumps({"version": "5.43", "themeCollection": {}}),
        "layoutOptimization": 0,
    }
    return json.dumps(layout, ensure_ascii=False).encode("utf-16-le")


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="json" ContentType="" />'
    '<Override PartName="/Version" ContentType="" />'
    '<Override PartName="/Connections" ContentType="" />'
    '<Override PartName="/Report/Layout" ContentType="" />'
    '<Override PartName="/Settings" ContentType="application/json" />'
    '<Override PartName="/Metadata" ContentType="application/json" />'
    "</Types>"
)


def build_thin_pbix(
    dataset_id: str,
    title: str,
    cols: list[Column],
    extra_charts: list[dict[str, Any]] | None = None,
    *,
    authoritative: bool = False,
) -> bytes:
    """A minimal live-connection .pbix bound to `dataset_id`, with visuals
    chosen from the column profile plus the user's own chart specs. The
    service rebinds after import anyway, so Connections just has to be
    plausible."""
    connections = {
        "Version": 1,
        "Connections": [
            {
                "Name": "EntityDataSource",
                "ConnectionString": (
                    "Data Source=pbiazure://api.powerbi.com;"
                    f"Initial Catalog={dataset_id};Integrated Security=ClaimsToken"
                ),
                "ConnectionType": "pbiServiceLive",
                "PbiServiceModelId": 1,
                "PbiModelVirtualServerName": "sobe_wowvirtualserver",
                "PbiModelDatabaseName": dataset_id,
            }
        ],
    }
    parts: dict[str, bytes] = {
        "Version": "1.28".encode("utf-16-le"),
        "[Content_Types].xml": _CONTENT_TYPES.encode("utf-8"),
        "Connections": json.dumps(connections).encode("utf-8"),
        "Report/Layout": _layout(
            title, pick_visuals(cols, extra_charts, authoritative=authoritative)
        ),
        "Metadata": json.dumps(
            {"Version": 3, "AutoCreatedRelationships": [], "FileDescription": ""}
        ).encode("utf-16-le"),
        "Settings": json.dumps({"Version": 1}).encode("utf-16-le"),
    }
    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as z:
        for name, content in parts.items():
            z.writestr(name, content)
    return buf.getvalue()


__all__ = ["build_thin_pbix", "pick_visuals"]
