"""Analysis Engine Phase 2 — the CleaningRecipe: typed, ordered, replayable ops.

Ops are data (JSON dicts), compiled here into ONE DuckDB SELECT and
materialized as a CLEANED COPY — the original file is never mutated, which is
what makes undo trivial and the recipe replayable on next week's file.

The default proposal only ever contains reversible normalizations (trim,
consolidate, coerce, dedupe). Imputation, outlier clipping, row filters and
column drops are EXPLICIT-ONLY: they exist as ops the user can ask for, are
validated against the file's own header, and imputation always attaches a
caveat — never silent, never the default.

Complexity: apply is one O(rows) streaming pass (plus one aggregate query per
impute/clip op); effect counting is one scan per op over the source.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from typing import Any

from sila.services.analysis.engine import _connect, _q
from sila.services.analysis.quality import _NUM_STRIP

# Canonical execution order — a recipe applies the same way regardless of the
# order the user picked ops in. Determinism is the replay guarantee.
CANONICAL_ORDER = (
    "trim",
    "consolidate_categories",
    "coerce_date",
    "coerce_number",
    "filter_rows",
    "clip_outliers",
    "impute",
    "drop_column",
    "dedupe",
)
EXPLICIT_ONLY_OPS = ("filter_rows", "clip_outliers", "impute", "drop_column")
_FILTER_OPS = {"eq": "=", "ne": "!=", "gt": ">", "lt": "<", "ge": ">=", "le": "<="}
_IMPUTE_STRATEGIES = ("median", "mean", "mode", "zero")


def _lit(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def propose_recipe(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The deterministic default recipe from a QualityReport — reversible
    normalizations only (no impute, no clip, no drops). Same report → same
    recipe, byte for byte."""
    ops: list[dict[str, Any]] = []
    ops += [{"op": "trim", "column": c} for c in sorted(report.get("whitespace") or [])]
    for entry in sorted(report.get("variants") or [], key=lambda e: e["column"]):
        mapping = {c["key"]: c["canonical"] for c in entry["clusters"]}
        ops.append({"op": "consolidate_categories", "column": entry["column"], "mapping": mapping})
    for cand in sorted(report.get("coercion") or [], key=lambda c: c["column"]):
        if cand["to"] == "date":
            ops.append({"op": "coerce_date", "column": cand["column"], "formats": cand["formats"]})
        else:
            ops.append({"op": "coerce_number", "column": cand["column"], "unit": cand.get("unit")})
    if report.get("duplicate_rows"):
        ops.append({"op": "dedupe"})
    return ops


def describe_op(op: dict[str, Any]) -> str:
    """Plain-English line for the user (values are ground truth)."""
    kind, col = str(op.get("op")), str(op.get("column", ""))
    if kind == "trim":
        return f"strip stray whitespace in '{col}'"
    if kind == "consolidate_categories":
        pairs = ", ".join(f"'{v}'" for v in list(op.get("mapping", {}).values())[:3])
        return f"merge case/spacing variants in '{col}' (→ {pairs})"
    if kind == "coerce_date":
        note = " (mixed formats)" if len(op.get("formats") or []) > 1 else ""
        return f"parse '{col}' as real dates{note}"
    if kind == "coerce_number":
        unit = op.get("unit")
        return f"parse '{col}' as numbers" + (f" (dropping the '{unit}' unit)" if unit else "")
    if kind == "dedupe":
        return "remove exact duplicate rows"
    if kind == "drop_column":
        return f"drop column '{col}'"
    if kind == "impute":
        return f"fill missing '{col}' with the {op.get('strategy', 'median')} (caveat attached)"
    if kind == "clip_outliers":
        return f"clip '{col}' outliers to the IQR fences"
    if kind == "filter_rows":
        return f"keep rows where {col} {op.get('cmp')} {op.get('value')}"
    return kind


def validate_ops(ops: list[dict[str, Any]], columns: list[str]) -> list[str]:
    """Errors for ops that don't fit this file — unknown op kinds, unknown
    columns, bad strategies. Empty list = valid."""
    errors: list[str] = []
    known = set(columns)
    for op in ops:
        kind = str(op.get("op", ""))
        if kind not in CANONICAL_ORDER:
            errors.append(f"unknown op '{kind}'")
            continue
        col = op.get("column")
        if kind != "dedupe" and (not col or str(col) not in known):
            errors.append(f"{kind}: unknown column '{col}'")
        if kind == "impute" and str(op.get("strategy", "median")) not in _IMPUTE_STRATEGIES:
            errors.append(f"impute: unknown strategy '{op.get('strategy')}'")
        if kind == "filter_rows" and str(op.get("cmp", "")) not in (
            *_FILTER_OPS,
            "is_null",
            "not_null",
        ):
            errors.append(f"filter_rows: unknown comparator '{op.get('cmp')}'")
    return errors


def _ordered(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        ops, key=lambda o: (CANONICAL_ORDER.index(str(o["op"])), str(o.get("column", "")))
    )


def _column_expr(expr: str, op: dict[str, Any], conn: Any, src_expr: str) -> str:
    """Fold one column-level op into the column's SELECT expression."""
    kind = str(op["op"])
    if kind == "trim":
        return f"trim({expr})"
    if kind == "consolidate_categories":
        whens = "".join(f" WHEN {_lit(k)} THEN {_lit(v)}" for k, v in sorted(op["mapping"].items()))
        return f"CASE lower(trim({expr})){whens} ELSE {expr} END"
    if kind == "coerce_date":
        tries = ", ".join(
            f"try_strptime(trim(CAST({expr} AS VARCHAR)), {_lit(f)})" for f in op["formats"]
        )
        return f"CAST(coalesce({tries}) AS DATE)"
    if kind == "coerce_number":
        return (
            f"try_cast(nullif(regexp_replace(trim(CAST({expr} AS VARCHAR)), "
            f"'{_NUM_STRIP}', '', 'g'), '') AS DOUBLE)"
        )
    if kind == "impute":
        strategy = str(op.get("strategy", "median"))
        if strategy == "zero":
            return f"coalesce({expr}, 0)"
        agg = {"median": "median", "mean": "avg", "mode": "mode"}[strategy]
        fill = conn.execute(f"SELECT {agg}({src_expr}) FROM dataset").fetchone()[0]
        if fill is None:
            return expr
        lit = _lit(str(fill)) if isinstance(fill, str) else repr(float(fill))
        return f"coalesce({expr}, {lit})"
    if kind == "clip_outliers":
        q1, q3 = conn.execute(
            f"SELECT quantile_cont({src_expr}, 0.25), quantile_cont({src_expr}, 0.75) FROM dataset"
        ).fetchone()
        if q1 is None or q3 is None:
            return expr
        iqr = float(q3) - float(q1)
        lo, hi = float(q1) - 1.5 * iqr, float(q3) + 1.5 * iqr
        return f"least(greatest({expr}, {lo!r}), {hi!r})"
    return expr


def _filter_clause(op: dict[str, Any], expr: str) -> str:
    cmp = str(op.get("cmp"))
    if cmp == "is_null":
        return f"{expr} IS NULL"
    if cmp == "not_null":
        return f"{expr} IS NOT NULL"
    raw = str(op.get("value", ""))
    try:
        value = repr(float(raw))
    except ValueError:
        value = _lit(raw)
    return f"{expr} {_FILTER_OPS[cmp]} {value}"


def _op_effect(conn: Any, op: dict[str, Any], expr_before: str, expr_after: str) -> int | None:
    """How many values this op touches — counted on the SOURCE, ground truth
    for the 'what changed' reply. None where the notion doesn't apply."""
    kind = str(op["op"])
    if kind in ("trim", "consolidate_categories", "clip_outliers"):
        return int(
            conn.execute(
                f"SELECT count(*) FROM dataset WHERE {expr_before} IS NOT NULL "
                f"AND CAST({expr_before} AS VARCHAR) != CAST({expr_after} AS VARCHAR)"
            ).fetchone()[0]
        )
    if kind in ("coerce_date", "coerce_number"):
        return int(
            conn.execute(
                f"SELECT count(*) FILTER (WHERE trim(CAST({expr_before} AS VARCHAR)) != '') "
                f"- count(*) FILTER (WHERE {expr_after} IS NOT NULL) FROM dataset"
            ).fetchone()[0]
        )  # values that FAILED to parse (became NULL) — loud, not hidden
    if kind == "impute":
        return int(
            conn.execute(f"SELECT count(*) FROM dataset WHERE {expr_before} IS NULL").fetchone()[0]
        )
    return None


def apply_recipe_to_file(src_path: str, ops: list[dict[str, Any]], dst_path: str) -> dict[str, Any]:
    """Materialize the cleaned copy at dst_path; return ground-truth effects.
    The source file is read-only throughout (undo = forget the copy)."""
    conn = _connect(src_path)
    try:
        described = conn.execute("DESCRIBE dataset").fetchall()
        columns = [str(name) for name, *_ in described]
        errors = validate_ops(ops, columns)
        if errors:
            raise ValueError("; ".join(errors))
        ordered = _ordered(ops)
        exprs = {c: _q(c) for c in columns}
        dropped: set[str] = set()
        wheres: list[str] = []
        dedupe = False
        effects: list[dict[str, Any]] = []
        for op in ordered:
            kind = str(op["op"])
            col = str(op.get("column", ""))
            before = exprs.get(col, _q(col) if col else "")
            if kind == "dedupe":
                dedupe = True
                continue
            if kind == "drop_column":
                dropped.add(col)
                effects.append({"describe": describe_op(op), "changed": None})
                continue
            if kind == "filter_rows":
                wheres.append(_filter_clause(op, before))
                effects.append({"describe": describe_op(op), "changed": None})
                continue
            after = _column_expr(before, op, conn, before)
            entry: dict[str, Any] = {
                "describe": describe_op(op),
                "changed": _op_effect(conn, op, before, after),
            }
            if kind == "impute":
                entry["caveat"] = (
                    f"'{col}' had missing values filled with the "
                    f"{op.get('strategy', 'median')} — treat claims about it with care"
                )
            effects.append(entry)
            exprs[col] = after
        kept = [c for c in columns if c not in dropped]
        select_cols = ", ".join(f"{exprs[c]} AS {_q(c)}" for c in kept)
        where = f" WHERE {' AND '.join(wheres)}" if wheres else ""
        # File order is preserved (and dedupe keeps the FIRST occurrence) so a
        # replayed recipe is byte-identical — DISTINCT would scramble rows.
        inner = f"SELECT {select_cols}, row_number() OVER () AS __rn FROM dataset{where}"
        out_cols = ", ".join(_q(c) for c in kept)
        qualify = (
            f" QUALIFY row_number() OVER (PARTITION BY {out_cols} ORDER BY __rn) = 1"
            if dedupe
            else ""
        )
        query = f"SELECT {out_cols} FROM ({inner}){qualify} ORDER BY __rn"
        rows_before = int(conn.execute("SELECT count(*) FROM dataset").fetchone()[0])
        rows_after = int(conn.execute(f"SELECT count(*) FROM ({query})").fetchone()[0])
        dst_lit = dst_path.replace("'", "''")
        conn.execute(f"COPY ({query}) TO '{dst_lit}' (HEADER, DELIMITER ',')")
        if dedupe:
            effects.append(
                {"describe": describe_op({"op": "dedupe"}), "changed": rows_before - rows_after}
            )
        return {
            "rows_before": rows_before,
            "rows_after": rows_after,
            "ops": effects,
            "caveats": [e["caveat"] for e in effects if e.get("caveat")],
        }
    finally:
        conn.close()


def apply_recipe_to_text(csv_text: str, ops: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """The attachment twin: text in → cleaned text out (same effects shape)."""
    fd, src_path = tempfile.mkstemp(suffix=".csv")
    dst_path = src_path + ".cleaned.csv"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as src:
            src.write(csv_text)
        effects = apply_recipe_to_file(src_path, ops, dst_path)
        with open(dst_path, encoding="utf-8") as fh:
            cleaned = fh.read()
        return _normalize_csv_text(cleaned), effects
    finally:
        for p in (src_path, dst_path):
            if os.path.exists(p):
                os.remove(p)


def _normalize_csv_text(text: str) -> str:
    """DuckDB quotes defensively on COPY; strip quoting the values don't need
    so cleaned attachments stay diff-friendly against their originals."""
    reader = csv.reader(io.StringIO(text))
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerows(reader)
    return out.getvalue()
