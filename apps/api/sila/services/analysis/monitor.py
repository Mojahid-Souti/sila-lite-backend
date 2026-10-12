"""Analysis Engine Phase 6 — monitor: schema contract, drift, alert rules.

The data contract from the spec: on ingest the schema is hashed; when the
same source is re-read (refresh/monitor), a schema change is SURFACED, never
swallowed — the refresh tool refuses to rebuild until the user acknowledges.
Distribution shift and volume anomalies are reported the same way, but only
warn (data grows; that's normal).

Alert rules are typed thresholds ({metric, agg, cmp, threshold}) stored on
the session and evaluated in DuckDB on every refresh. Everything here is
exportable via `export_monitor_config` so the user-owned agent copy inherits
objective + cleaning recipe + semantic layer + chart list + alert rules.

Complexity: O(columns) per baseline/drift; one aggregate query per alert.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sila.services.analysis.engine import _connect, _q, profile_dataset

_VOLUME_LOW, _VOLUME_HIGH = 0.5, 2.0  # outside → volume anomaly flag
_SHIFT_SCORE = 1.0  # |Δmean| / std_old above this → distribution shift flag
_ALERT_CMPS = {"gt": ">", "lt": "<", "ge": ">=", "le": "<="}
_ALERT_AGGS = ("avg", "sum", "min", "max", "count")
_MAX_RULES = 10


def schema_hash(columns: list[dict[str, Any]]) -> str:
    basis = "|".join(f"{c['name']}:{c['kind']}" for c in columns)
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def capture_baseline(path: str) -> dict[str, Any]:
    """The monitoring baseline: schema fingerprint + row count + a mean/std
    summary per numeric column (the drift yardstick). One pass over the file."""
    prof = profile_dataset(path)
    columns = prof["columns"]
    metrics: dict[str, dict[str, float]] = {}
    numeric = [c["name"] for c in columns if c["kind"] == "number"]
    if numeric:
        conn = _connect(path)
        try:
            sel = ", ".join(f"avg({_q(c)}), coalesce(stddev_samp({_q(c)}), 0)" for c in numeric)
            row = conn.execute(f"SELECT {sel} FROM dataset").fetchone()
            for i, name in enumerate(numeric):
                mean, std = row[2 * i], row[2 * i + 1]
                if mean is not None:
                    metrics[name] = {"mean": round(float(mean), 4), "std": round(float(std), 4)}
        finally:
            conn.close()
    return {
        "hash": schema_hash(columns),
        "columns": columns,
        "rows": int(prof["rows"]),
        "metrics": metrics,
    }


def drift_report(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compare a fresh baseline against the stored one. `schema_changed` is
    the hard gate; volume/shift entries are warnings to relay."""
    old_cols = {c["name"]: c["kind"] for c in baseline.get("columns") or []}
    new_cols = {c["name"]: c["kind"] for c in current.get("columns") or []}
    changes: list[str] = []
    for name in sorted(old_cols.keys() - new_cols.keys()):
        changes.append(f"column '{name}' disappeared")
    for name in sorted(new_cols.keys() - old_cols.keys()):
        changes.append(f"new column '{name}' appeared")
    for name in sorted(old_cols.keys() & new_cols.keys()):
        if old_cols[name] != new_cols[name]:
            changes.append(f"'{name}' changed type: {old_cols[name]} → {new_cols[name]}")

    rows_before = int(baseline.get("rows") or 0)
    rows_after = int(current.get("rows") or 0)
    ratio = (rows_after / rows_before) if rows_before else None
    volume_flag = ratio is not None and not (_VOLUME_LOW <= ratio <= _VOLUME_HIGH)

    shifts: list[dict[str, Any]] = []
    old_metrics = baseline.get("metrics") or {}
    new_metrics = current.get("metrics") or {}
    for name in sorted(old_metrics.keys() & new_metrics.keys()):
        old_m, new_m = old_metrics[name], new_metrics[name]
        std = float(old_m.get("std") or 0.0)
        if std <= 0:
            continue
        score = abs(float(new_m["mean"]) - float(old_m["mean"])) / std
        if score >= _SHIFT_SCORE:
            shifts.append(
                {
                    "column": name,
                    "mean_before": old_m["mean"],
                    "mean_after": new_m["mean"],
                    "shift_score": round(score, 2),
                }
            )
    return {
        "schema_changed": bool(changes),
        "changes": changes,
        "volume": {
            "rows_before": rows_before,
            "rows_after": rows_after,
            "flag": volume_flag,
        },
        "shifts": shifts,
        "clean": not changes and not volume_flag and not shifts,
    }


def describe_drift(report: dict[str, Any]) -> str:
    """Plain-language drift lines (values are ground truth)."""
    lines: list[str] = []
    for change in report.get("changes") or []:
        lines.append(f"SCHEMA: {change}")
    vol = report.get("volume") or {}
    if vol.get("flag"):
        lines.append(f"VOLUME: rows went {vol['rows_before']:,} → {vol['rows_after']:,}")
    for s in report.get("shifts") or []:
        lines.append(
            f"SHIFT: '{s['column']}' mean moved {s['mean_before']:g} → {s['mean_after']:g} "
            f"({s['shift_score']:g}σ of the old spread)"
        )
    return "\n".join(lines) if lines else "No drift against the baseline."


def normalize_rule(args: dict[str, Any]) -> dict[str, Any] | None:
    """A typed alert rule from tool args, or None when invalid."""
    metric = str(args.get("metric", "")).strip()
    cmp = str(args.get("cmp", "")).strip().lower()
    agg = str(args.get("agg", "avg")).strip().lower() or "avg"
    try:
        threshold = float(args.get("threshold"))
    except (TypeError, ValueError):
        return None
    if not metric or cmp not in _ALERT_CMPS or agg not in _ALERT_AGGS:
        return None
    return {"metric": metric, "agg": agg, "cmp": cmp, "threshold": threshold}


def describe_rule(rule: dict[str, Any]) -> str:
    return f"{rule['agg']}({rule['metric']}) {_ALERT_CMPS[rule['cmp']]} {rule['threshold']:g}"


def evaluate_alerts(path: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate every rule against the CURRENT file (in code, in DuckDB).
    Returns one entry per rule: the computed value + whether it triggered;
    an unmatchable metric is a loud error entry, never silence."""
    if not rules:
        return []
    prof = profile_dataset(path)
    names = {c["name"].lower(): c["name"] for c in prof["columns"]}
    conn = _connect(path)
    results: list[dict[str, Any]] = []
    try:
        for rule in rules:
            wanted = str(rule.get("metric", "")).lower()
            column = names.get(wanted) or next(
                (v for k, v in names.items() if wanted and wanted in k), None
            )
            if column is None:
                results.append(
                    {"rule": rule, "error": f"column '{rule.get('metric')}' not in this file"}
                )
                continue
            value = conn.execute(f"SELECT {rule['agg']}({_q(column)}) FROM dataset").fetchone()[0]
            if value is None:
                results.append({"rule": rule, "error": f"'{column}' has no values"})
                continue
            value = float(value)
            threshold = float(rule["threshold"])
            triggered = {
                "gt": value > threshold,
                "lt": value < threshold,
                "ge": value >= threshold,
                "le": value <= threshold,
            }[rule["cmp"]]
            results.append({"rule": rule, "value": round(value, 4), "triggered": triggered})
    finally:
        conn.close()
    return results


def export_monitor_config(state: dict[str, Any]) -> dict[str, Any]:
    """The portable pipeline the user-owned agent copy inherits: objective,
    cleaning recipe, semantic layer, chart recipes, alert rules, baseline."""
    charts = (state.get("last_dashboard") or {}).get("charts") or []
    recipe_keys = ("kind", "title", "metric", "by", "agg", "columns", "series_cols")
    return {
        "objective": state.get("analysis_objective"),
        "cleaning_recipe": state.get("cleaning_recipe") or {},
        "semantic_layer": state.get("semantic_layer") or [],
        "charts": [
            {k: c[k] for k in recipe_keys if c.get(k) is not None}
            for c in charts
            if isinstance(c, dict)
        ],
        "alert_rules": state.get("alert_rules") or [],
        "baseline": state.get("monitor_baseline"),
    }
