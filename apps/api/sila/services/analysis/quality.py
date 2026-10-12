"""Analysis Engine Phase 2 — the QualityReport: full-file data profiling.

Everything here is computed by DuckDB over the ENTIRE file (never a head
sample) and returned as ground truth for the model to quote. Outliers are
FLAGGED, never deleted; imputation is never proposed — the report only
surfaces what a human should decide about.

Complexity: O(rows) per check as streaming SQL scans; the variant clustering
is O(distinct values) and only runs on category-sized columns (≤50 distinct).
"""

from __future__ import annotations

from typing import Any

from sila.services.analysis.engine import _connect, _q

# Fixed priority order — determinism is what makes the proposed recipe exact.
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y")
_COERCE_RATE = 0.8  # a coercion is only proposed when ≥80% of values parse
_MIXED_RATE = 0.1  # a second date format ≥10% marks the column mixed-format
_VARIANT_MAX_DISTINCT = 50  # cluster only category-sized columns, not free text
_NUM_STRIP = r"[^0-9.eE+-]"


def _nonnull(conn: Any, col: str) -> int:
    q = _q(col)
    return int(
        conn.execute(
            f"SELECT count(*) FROM dataset "
            f"WHERE {q} IS NOT NULL AND trim(CAST({q} AS VARCHAR)) != ''"
        ).fetchone()[0]
    )


def _date_candidate(conn: Any, col: str, nonnull: int) -> dict[str, Any] | None:
    """Greedy format cover: add a format only when it parses rows the chosen
    ones don't — yields a deterministic, minimal format list."""
    if nonnull == 0:
        return None
    q = f"trim(CAST({_q(col)} AS VARCHAR))"
    chosen: list[str] = []
    covered = 0
    for fmt in DATE_FORMATS:
        attempt = [*chosen, fmt]
        parts = ", ".join(f"try_strptime({q}, '{f}')" for f in attempt)
        n = int(
            conn.execute(
                f"SELECT count(*) FROM dataset WHERE coalesce({parts}) IS NOT NULL"
            ).fetchone()[0]
        )
        if n > covered:
            chosen.append(fmt)
            covered = n
    rate = covered / nonnull
    if not chosen or rate < _COERCE_RATE:
        return None
    return {
        "column": col,
        "to": "date",
        "rate": round(rate, 3),
        "formats": chosen,
        "mixed_formats": len(chosen) > 1,
    }


def _number_candidate(conn: Any, col: str, nonnull: int) -> dict[str, Any] | None:
    if nonnull == 0:
        return None
    q = f"trim(CAST({_q(col)} AS VARCHAR))"
    stripped = f"try_cast(nullif(regexp_replace({q}, '{_NUM_STRIP}', '', 'g'), '') AS DOUBLE)"
    parsed, raw_numeric = conn.execute(
        f"SELECT count(*) FILTER (WHERE {stripped} IS NOT NULL), "
        f"count(*) FILTER (WHERE try_cast({q} AS DOUBLE) IS NOT NULL) FROM dataset"
    ).fetchone()
    rate = int(parsed) / nonnull
    if rate < _COERCE_RATE or int(raw_numeric) == int(parsed):
        return None  # already numeric as-is → the sniffer would have typed it
    unit_row = conn.execute(
        f"SELECT regexp_extract({q}, '([A-Za-z%]+)\\s*$', 1) AS u, count(*) AS n "
        f"FROM dataset WHERE {q} IS NOT NULL AND regexp_extract({q}, '([A-Za-z%]+)\\s*$', 1) != ''"
        " GROUP BY 1 ORDER BY n DESC, u LIMIT 1"
    ).fetchone()
    unit = str(unit_row[0]) if unit_row and int(unit_row[1]) >= nonnull * 0.5 else None
    return {"column": col, "to": "number", "rate": round(rate, 3), "unit": unit}


def _variant_clusters(conn: Any, col: str) -> list[dict[str, Any]]:
    """Near-duplicate categories via normalized-string grouping: canonical is
    the most frequent RAW variant (ties → alphabetical)."""
    rows = conn.execute(
        f"SELECT lower(trim({_q(col)})) AS k, {_q(col)} AS raw, count(*) AS n FROM dataset "
        f"WHERE {_q(col)} IS NOT NULL GROUP BY 1, 2 ORDER BY k, n DESC, raw"
    ).fetchall()
    by_key: dict[str, list[tuple[str, int]]] = {}
    for key, raw, n in rows:
        by_key.setdefault(str(key), []).append((str(raw), int(n)))
    clusters = []
    for key, variants in sorted(by_key.items()):
        if len(variants) > 1:
            canonical = max(variants, key=lambda v: (v[1], v[0]))[0]
            clusters.append(
                {
                    "key": key,
                    "canonical": canonical,
                    "variants": [v for v, _ in variants],
                    "count": sum(n for _, n in variants),
                }
            )
    return clusters


def _score(report: dict[str, Any], n_cols: int) -> tuple[int, list[dict[str, Any]]]:
    """Deterministic 0–100 score; the deductions carry the 'why'."""
    rows = max(1, int(report["rows"]))
    missing_avg = sum(report["missing"].values()) / max(1, len(report["missing"]))
    candidates = [
        (
            "column types need fixing (dates/numbers stored as text)",
            round(20 * len(report["coercion"]) / max(1, n_cols)),
        ),
        ("duplicate rows", round(25 * report["duplicate_rows"] / rows)),
        ("missing values", round(30 * missing_avg)),
        ("near-duplicate category spellings", min(10, 3 * len(report["variants"]))),
        ("stray whitespace in text columns", min(5, len(report["whitespace"]))),
        ("outlier values flagged", min(5, len([c for c, n in report["outliers"].items() if n]))),
        ("constant columns carry no information", min(5, 2 * len(report["constant"]))),
    ]
    deductions = sorted(
        [{"reason": r, "points": p} for r, p in candidates if p > 0],
        key=lambda d: (-d["points"], d["reason"]),
    )
    return max(0, 100 - sum(d["points"] for d in deductions)), deductions


def quality_report(path: str) -> dict[str, Any]:
    """The full QualityReport over the ENTIRE file. Raises ValueError on an
    unreadable file (same contract as the engine)."""
    try:
        conn = _connect(path)
    except Exception as exc:  # noqa: BLE001 — surface as the standard contract
        raise ValueError(f"couldn't read the dataset: {exc}") from exc
    try:
        rows = int(conn.execute("SELECT count(*) FROM dataset").fetchone()[0])
        described = conn.execute("DESCRIBE dataset").fetchall()
        cols = [(str(name), str(dtype).upper()) for name, dtype, *_ in described]
        report: dict[str, Any] = {
            "rows": rows,
            "columns": [c for c, _ in cols],
            "missing": {},
            "coercion": [],
            "whitespace": [],
            "variants": [],
            "constant": [],
            "outliers": {},
            "duplicate_rows": 0,
        }
        if rows == 0:
            report["score"], report["deductions"] = (
                0,
                [{"reason": "the file has no data rows", "points": 100}],
            )
            return report
        distinct_all = int(
            conn.execute("SELECT count(*) FROM (SELECT DISTINCT * FROM dataset)").fetchone()[0]
        )
        report["duplicate_rows"] = rows - distinct_all
        for col, dtype in cols:
            q = _q(col)
            nonnull = _nonnull(conn, col)
            report["missing"][col] = round(1 - nonnull / rows, 3)
            distinct = int(conn.execute(f"SELECT count(DISTINCT {q}) FROM dataset").fetchone()[0])
            if distinct <= 1:
                report["constant"].append(col)
                continue
            if dtype.startswith("VARCHAR"):
                ws = int(
                    conn.execute(f"SELECT count(*) FROM dataset WHERE {q} != trim({q})").fetchone()[
                        0
                    ]
                )
                if ws:
                    report["whitespace"].append(col)
                cand = _date_candidate(conn, col, nonnull) or _number_candidate(conn, col, nonnull)
                if cand:
                    report["coercion"].append(cand)
                elif distinct <= _VARIANT_MAX_DISTINCT:
                    clusters = _variant_clusters(conn, col)
                    if clusters:
                        report["variants"].append({"column": col, "clusters": clusters})
            elif any(t in dtype for t in ("INT", "DOUBLE", "FLOAT", "DECIMAL", "HUGE")):
                q1, q3 = conn.execute(
                    f"SELECT quantile_cont({q}, 0.25), quantile_cont({q}, 0.75) FROM dataset"
                ).fetchone()
                if q1 is not None and q3 is not None:
                    iqr = float(q3) - float(q1)
                    lo, hi = float(q1) - 1.5 * iqr, float(q3) + 1.5 * iqr
                    n_out = int(
                        conn.execute(
                            f"SELECT count(*) FROM dataset WHERE {q} < {lo} OR {q} > {hi}"
                        ).fetchone()[0]
                    )
                    if n_out:
                        report["outliers"][col] = n_out
        report["score"], report["deductions"] = _score(report, len(cols))
        return report
    finally:
        conn.close()
