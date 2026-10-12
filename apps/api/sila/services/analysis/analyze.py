"""Analysis Engine Phase 3 — comparative / relational / driver / segment tools.

Every function reads the FULL file through DuckDB aggregates, runs the math in
`stats`, and returns typed Findings whose phrasing is rendered in code. The
model receives only statement slots and phrasing — never rows to summarize.

Test auto-selection (deterministic, documented):
  numeric metric × 2 groups   → Welch t; Mann-Whitney when either group is
                                heavily skewed (|skew| > 2) or small (< 15)
  numeric metric × k>2 groups → one-way ANOVA (+ pairwise Welch, Holm-adjusted)
  category × category         → chi-square independence + Cramér's V

Complexity: O(rows) per SQL aggregate; O(groups²) only over ≤ 12 groups.
"""

from __future__ import annotations

import math
from typing import Any

from sila.services.analysis import stats
from sila.services.analysis.engine import _connect, _q
from sila.services.analysis.findings import Finding, make_finding, simpson_check

_MAX_GROUPS = 12
_SKEW_LIMIT = 2.0
_SMALL_GROUP = 15
_MAX_CORR_COLS = 8


def _effect_label(effect: float) -> str:
    """Cohen-style plain-language magnitude for the prose (no numbers added —
    the guard basis stays intact)."""
    e = abs(effect)
    if e >= 1.2:
        return "very large"
    if e >= 0.8:
        return "large"
    if e >= 0.5:
        return "moderate"
    if e >= 0.2:
        return "small"
    return "negligible"


def _typed_columns(conn: Any) -> dict[str, str]:
    return {
        str(name): str(dtype).upper()
        for name, dtype, *_ in conn.execute("DESCRIBE dataset").fetchall()
    }


def _is_numeric(dtype: str) -> bool:
    return any(t in dtype for t in ("INT", "DOUBLE", "FLOAT", "DECIMAL", "HUGE"))


def _group_aggregates(
    conn: Any, metric: str, by: str
) -> list[tuple[str, int, float, float, float]]:
    """(group, n, mean, variance, skewness) for the top groups by size."""
    rows = conn.execute(
        f"SELECT CAST({_q(by)} AS VARCHAR), count(*), avg({_q(metric)}), "
        f"coalesce(var_samp({_q(metric)}), 0), coalesce(skewness({_q(metric)}), 0) "
        f"FROM dataset WHERE {_q(metric)} IS NOT NULL AND {_q(by)} IS NOT NULL "
        f"GROUP BY 1 ORDER BY 2 DESC LIMIT {_MAX_GROUPS}"
    ).fetchall()
    return [(str(g), int(n), float(m), float(v), float(s)) for g, n, m, v, s in rows]


def _mann_whitney_from_sql(
    conn: Any, metric: str, by: str, group_a: str, group_b: str
) -> tuple[float, float, float, int, int]:
    """(U, rank_biserial, p, n_a, n_b) — average ranks + tie term in SQL."""
    q_m, q_b = _q(metric), _q(by)
    esc = str.maketrans({"'": "''"})
    pair = f"({q_b} = '{group_a.translate(esc)}' OR {q_b} = '{group_b.translate(esc)}')"
    base = (
        f"SELECT CAST({q_b} AS VARCHAR) AS g, {q_m} AS v FROM dataset "
        f"WHERE {q_m} IS NOT NULL AND {pair}"
    )
    rows = conn.execute(
        f"WITH vals AS ({base}), ranked AS ("
        "  SELECT g, rank() OVER (ORDER BY v) + (count(*) OVER (PARTITION BY v) - 1) / 2.0 AS r"
        "  FROM vals)"
        " SELECT g, sum(r), count(*) FROM ranked GROUP BY 1"
    ).fetchall()
    sums = {str(g): (float(s), int(n)) for g, s, n in rows}
    tie = float(
        conn.execute(
            f"WITH vals AS ({base}) "
            "SELECT coalesce(sum(power(tc, 3) - tc), 0) FROM "
            "(SELECT count(*) AS tc FROM vals GROUP BY v)"
        ).fetchone()[0]
    )
    rank_sum_a, n_a = sums.get(group_a, (0.0, 0))
    _, n_b = sums.get(group_b, (0.0, 0))
    u, r, p = stats.mann_whitney(n_a, n_b, rank_sum_a, tie)
    return u, r, p, n_a, n_b


def _simpson_caveat(
    conn: Any, metric: str, by: str, segment: str, group_a: str, group_b: str, overall_diff: float
) -> str | None:
    esc = str.maketrans({"'": "''"})
    a_lit, b_lit = group_a.translate(esc), group_b.translate(esc)
    rows = conn.execute(
        f"SELECT CAST({_q(segment)} AS VARCHAR), "
        f"avg({_q(metric)}) FILTER (WHERE {_q(by)} = '{a_lit}'), "
        f"avg({_q(metric)}) FILTER (WHERE {_q(by)} = '{b_lit}'), count(*) "
        f"FROM dataset WHERE {_q(segment)} IS NOT NULL AND "
        f"({_q(by)} = '{a_lit}' OR {_q(by)} = '{b_lit}') GROUP BY 1"
    ).fetchall()
    total = sum(int(n) for *_, n in rows)
    diffs = {
        f"{segment}={seg}": (float(ma) - float(mb), int(n))
        for seg, ma, mb, n in rows
        if ma is not None and mb is not None
    }
    return simpson_check(overall_diff, diffs, total)


def compare_groups(
    path: str,
    metric: str,
    by: str,
    segment: str | None = None,
    objective: dict[str, Any] | None = None,
) -> list[Finding]:
    """Group differences with the correct test auto-selected, Holm correction
    across every p-value actually computed, and the Simpson check."""
    conn = _connect(path)
    try:
        types = _typed_columns(conn)
        if not _is_numeric(types.get(metric, "")):
            return [_chi_square_finding(conn, metric, by, objective)]
        groups = _group_aggregates(conn, metric, by)
        if len(groups) < 2:
            raise ValueError(f"'{by}' has fewer than two groups with '{metric}' values")
        evidence = {
            "kind": "bar",
            "title": f"{metric} by {by} (avg)",
            "data": [{"name": g, "value": round(m, 2)} for g, _, m, _, _ in groups],
            "metric": metric,
            "by": by,
            "agg": "avg",
        }
        raw_p: list[float] = []
        anova_slots: dict[str, Any] | None = None
        if len(groups) > 2:
            f_stat, _, _, p_f = stats.anova_oneway([(n, m, v) for _, n, m, v, _ in groups])
            raw_p.append(p_f)
            anova_slots = {
                "F": round(f_stat, 3),
                "eta_squared": round(stats.eta_squared([(n, m, v) for _, n, m, v, _ in groups]), 4),
            }
        # Headline pair: highest vs lowest mean.
        by_mean = sorted(groups, key=lambda g: g[2])
        lo, hi = by_mean[0], by_mean[-1]
        use_mw = min(lo[1], hi[1]) < _SMALL_GROUP or max(abs(lo[4]), abs(hi[4])) > _SKEW_LIMIT
        if use_mw:
            _, effect, p_pair, n_a, n_b = _mann_whitney_from_sql(conn, metric, by, hi[0], lo[0])
            test_name, effect_name = "Mann-Whitney", "rank-biserial"
            pair_n = n_a + n_b
        else:
            _, _, p_pair = stats.welch_t(hi[1], hi[2], hi[3], lo[1], lo[2], lo[3])
            effect = stats.cohens_d(hi[1], hi[2], hi[3], lo[1], lo[2], lo[3])
            test_name, effect_name = "Welch t", "Cohen's d"
            pair_n = hi[1] + lo[1]
        raw_p.append(p_pair)
        adjusted = stats.holm(raw_p)
        p_pair_adj = adjusted[-1]
        caveats: list[str] = []
        if segment:
            caveat = _simpson_caveat(conn, metric, by, segment, hi[0], lo[0], hi[2] - lo[2])
            if caveat:
                caveats.append(caveat)
        rel = (hi[2] - lo[2]) / abs(lo[2]) * 100 if lo[2] else math.inf
        slots = {
            "metric": metric,
            "by": by,
            "group_high": hi[0],
            "mean_high": round(hi[2], 3),
            "group_low": lo[0],
            "mean_low": round(lo[2], 3),
            "relative_difference_pct": round(rel, 1) if math.isfinite(rel) else None,
            "test": test_name,
            "effect_size_name": effect_name,
        }
        # Plain-language magnitude in the prose; the raw effect value stays in
        # the slots — "rank-biserial=-1.00" read as noise to a non-analyst.
        phrasing = (
            f"{hi[0]} averages {slots['mean_high']:g} {metric} vs {lo[0]}'s "
            f"{slots['mean_low']:g}"
            + (f" — {slots['relative_difference_pct']:g}% higher" if lo[2] else "")
            + f" (a {_effect_label(effect)} gap, {test_name} test)"
        )
        out = [
            make_finding(
                "difference",
                slots,
                phrasing,
                effect_size=effect,
                p_value=p_pair,
                p_adjusted=p_pair_adj,
                n=pair_n,
                tests_run=len(raw_p),
                caveats=caveats,
                evidence=evidence,
                objective=objective,
            )
        ]
        if anova_slots:
            out.append(
                make_finding(
                    "difference",
                    {"metric": metric, "by": by, **anova_slots, "groups": len(groups)},
                    (
                        f"{metric} differs across the {len(groups)} {by} groups "
                        f"(ANOVA F={anova_slots['F']:g}, "
                        f"eta²={anova_slots['eta_squared']:g})"
                    ),
                    effect_size=anova_slots["eta_squared"],
                    p_value=raw_p[0],
                    p_adjusted=adjusted[0],
                    n=sum(g[1] for g in groups),
                    tests_run=len(raw_p),
                    evidence=evidence,
                    objective=objective,
                )
            )
        return out
    finally:
        conn.close()


def _chi_square_finding(
    conn: Any, metric: str, by: str, objective: dict[str, Any] | None
) -> Finding:
    rows = conn.execute(
        f"SELECT CAST({_q(metric)} AS VARCHAR), CAST({_q(by)} AS VARCHAR), count(*) "
        f"FROM dataset WHERE {_q(metric)} IS NOT NULL AND {_q(by)} IS NOT NULL GROUP BY 1, 2"
    ).fetchall()
    levels_a = sorted({r[0] for r in rows})[:_MAX_GROUPS]
    levels_b = sorted({r[1] for r in rows})[:_MAX_GROUPS]
    counts = {(a, b): int(n) for a, b, n in rows}
    table = [[counts.get((a, b), 0) for b in levels_b] for a in levels_a]
    chi2, df, p, v = stats.chi2_independence([[float(x) for x in row] for row in table])
    n = sum(sum(row) for row in table)
    return make_finding(
        "difference",
        {
            "metric": metric,
            "by": by,
            "chi2": round(chi2, 3),
            "df": int(df),
            "cramers_v": round(v, 4),
        },
        f"'{metric}' and '{by}' are associated (chi²={chi2:.2f}, Cramér's V={v:.2f})",
        effect_size=v,
        p_value=p,
        p_adjusted=p,
        n=int(n),
        objective=objective,
    )


def correlations(path: str, objective: dict[str, Any] | None = None) -> list[Finding]:
    """Pearson + Spearman over numeric column pairs, Holm-adjusted; the
    causation caveat is attached by construction and cannot be stripped."""
    from sila.services.sila.data_dashboard import _numeric_id_like

    conn = _connect(path)
    try:
        types = _typed_columns(conn)
        nums = [c for c, t in types.items() if _is_numeric(t) and not _numeric_id_like(c, [])]
        nums = nums[:_MAX_CORR_COLS]
        if len(nums) < 2:
            raise ValueError("needs at least two numeric columns to correlate")
        pairs: list[dict[str, Any]] = []
        for i, x in enumerate(nums):
            for y in nums[i + 1 :]:
                where = f"WHERE {_q(x)} IS NOT NULL AND {_q(y)} IS NOT NULL"
                r, n = conn.execute(
                    f"SELECT coalesce(corr({_q(x)}, {_q(y)}), 0), count(*) FROM dataset {where}"
                ).fetchone()
                rho = conn.execute(
                    f"SELECT coalesce(corr(rx, ry), 0) FROM ("
                    f"SELECT rank() OVER (ORDER BY {_q(x)}) AS rx, "
                    f"rank() OVER (ORDER BY {_q(y)}) AS ry FROM dataset {where})"
                ).fetchone()[0]
                pairs.append(
                    {
                        "x": x,
                        "y": y,
                        "r": float(r),
                        "rho": float(rho),
                        "n": int(n),
                        "p": stats.corr_p(float(r), int(n)),
                    }
                )
        adjusted = stats.holm([p["p"] for p in pairs])
        for pair, adj in zip(pairs, adjusted, strict=True):
            pair["p_adj"] = adj
        top = sorted(pairs, key=lambda p: -abs(p["r"]))[:3]
        out = []
        for pair in top:
            caveats = []
            if abs(pair["r"]) > 0.9:
                caveats.append(
                    f"'{pair['x']}' and '{pair['y']}' carry near-duplicate information "
                    "(multicollinearity) — don't treat them as independent signals"
                )
            direction = "positively" if pair["r"] > 0 else "negatively"
            out.append(
                make_finding(
                    "correlation",
                    {
                        "x": pair["x"],
                        "y": pair["y"],
                        "pearson_r": round(pair["r"], 4),
                        "spearman_rho": round(pair["rho"], 4),
                    },
                    (
                        f"'{pair['x']}' and '{pair['y']}' move {direction} together "
                        f"(r={pair['r']:.2f}, Spearman ρ={pair['rho']:.2f})"
                    ),
                    effect_size=abs(pair["r"]),
                    p_value=pair["p"],
                    p_adjusted=pair["p_adj"],
                    n=pair["n"],
                    tests_run=len(pairs),
                    caveats=caveats,
                    objective=objective,
                )
            )
        return out
    finally:
        conn.close()


def concentration(
    path: str, metric: str, by: str, objective: dict[str, Any] | None = None
) -> list[Finding]:
    """Pareto / top-N share / HHI over the metric's distribution across groups."""
    conn = _connect(path)
    try:
        rows = conn.execute(
            f"SELECT CAST({_q(by)} AS VARCHAR), sum({_q(metric)}) AS s FROM dataset "
            f"WHERE {_q(metric)} IS NOT NULL AND {_q(by)} IS NOT NULL "
            "GROUP BY 1 HAVING sum(" + _q(metric) + ") > 0 ORDER BY s DESC"
        ).fetchall()
        total = sum(float(s) for _, s in rows)
        if total <= 0 or not rows:
            raise ValueError(f"'{metric}' has no positive totals to measure concentration")
        shares = [float(s) / total for _, s in rows]
        hhi = sum(s * s for s in shares)
        top5 = sum(shares[:5])
        cum, pareto_k = 0.0, 0
        for share in shares:
            cum += share
            pareto_k += 1
            if cum >= 0.8:
                break
        label = "highly" if hhi > 0.25 else "moderately" if hhi > 0.15 else "not very"
        n = int(conn.execute(f"SELECT count({_q(metric)}) FROM dataset").fetchone()[0])
        slots = {
            "metric": metric,
            "by": by,
            "groups": len(rows),
            "top_group": str(rows[0][0]),
            "top_share_pct": round(shares[0] * 100, 1),
            "top5_share_pct": round(top5 * 100, 1),
            "hhi": round(hhi, 4),
            "pareto_groups_for_80pct": pareto_k,
        }
        return [
            make_finding(
                "concentration",
                slots,
                (
                    f"{slots['top_group']} alone holds {slots['top_share_pct']:g}% of total "
                    f"{metric}; {pareto_k} of {len(rows)} {by} groups cover 80% "
                    f"(HHI {hhi:.2f} — {label} concentrated)"
                ),
                effect_size=hhi,
                n=n,
                objective=objective,
            )
        ]
    finally:
        conn.close()


def drivers(path: str, target: str, objective: dict[str, Any] | None = None) -> list[Finding]:
    """What moves the target: normalized mutual information of each feature
    against the binned target. Association only — the caveat is structural."""
    from sila.services.sila.data_dashboard import _numeric_id_like

    conn = _connect(path)
    try:
        types = _typed_columns(conn)
        if not _is_numeric(types.get(target, "")):
            raise ValueError(f"'{target}' must be a numeric column")
        scores: list[tuple[str, float]] = []
        n = int(
            conn.execute(f"SELECT count(*) FROM dataset WHERE {_q(target)} IS NOT NULL").fetchone()[
                0
            ]
        )
        for col, dtype in types.items():
            if col == target or _numeric_id_like(col, []):
                continue
            if _is_numeric(dtype):
                feature = f"CAST(ntile(4) OVER (ORDER BY {_q(col)}) AS VARCHAR)"
            else:
                distinct = int(
                    conn.execute(f"SELECT count(DISTINCT {_q(col)}) FROM dataset").fetchone()[0]
                )
                if distinct > 24 or distinct < 2:
                    continue
                feature = f"CAST({_q(col)} AS VARCHAR)"
            joint = conn.execute(
                f"SELECT fb, tb, count(*) FROM (SELECT {feature} AS fb, "
                f"ntile(4) OVER (ORDER BY {_q(target)}) AS tb FROM dataset "
                f"WHERE {_q(col)} IS NOT NULL AND {_q(target)} IS NOT NULL) GROUP BY 1, 2"
            ).fetchall()
            mi = _normalized_mi([(str(a), int(b), int(c)) for a, b, c in joint])
            scores.append((col, mi))
        if not scores:
            raise ValueError("no usable feature columns to rank against the target")
        top = sorted(scores, key=lambda s: -s[1])[:3]
        ranked = ", ".join(f"{c} ({s:.2f})" for c, s in top)
        return [
            make_finding(
                "driver",
                {
                    "target": target,
                    "ranked_drivers": [{"column": c, "mi": round(s, 4)} for c, s in top],
                },
                f"the strongest associations with {target} are: {ranked} "
                "(normalized mutual information)",
                effect_size=top[0][1],
                n=n,
                objective=objective,
            )
        ]
    finally:
        conn.close()


def _normalized_mi(joint: list[tuple[str, int, int]]) -> float:
    total = sum(c for *_, c in joint)
    if total == 0:
        return 0.0
    px: dict[str, float] = {}
    py: dict[int, float] = {}
    for a, b, c in joint:
        px[a] = px.get(a, 0.0) + c / total
        py[b] = py.get(b, 0.0) + c / total
    mi = sum((c / total) * math.log((c / total) / (px[a] * py[b])) for a, b, c in joint if c > 0)
    hx = -sum(p * math.log(p) for p in px.values() if p > 0)
    hy = -sum(p * math.log(p) for p in py.values() if p > 0)
    denom = min(hx, hy)
    return mi / denom if denom > 0 else 0.0


def segments(path: str, metric: str, objective: dict[str, Any] | None = None) -> list[Finding]:
    """Which segments explain the variation: the categorical split with the
    best variance reduction, as human-readable rules — never cluster ids."""
    conn = _connect(path)
    try:
        types = _typed_columns(conn)
        if not _is_numeric(types.get(metric, "")):
            raise ValueError(f"'{metric}' must be a numeric column")
        overall, var_total, n = conn.execute(
            f"SELECT avg({_q(metric)}), coalesce(var_samp({_q(metric)}), 0), count({_q(metric)}) "
            "FROM dataset"
        ).fetchone()
        overall, var_total, n = float(overall or 0), float(var_total), int(n)
        if var_total <= 0:
            raise ValueError(f"'{metric}' doesn't vary — nothing to segment")
        best: tuple[str, float, list[tuple[str, int, float]]] | None = None
        for col, dtype in types.items():
            if col == metric or _is_numeric(dtype):
                continue
            distinct = int(
                conn.execute(f"SELECT count(DISTINCT {_q(col)}) FROM dataset").fetchone()[0]
            )
            if distinct > 24 or distinct < 2:
                continue
            groups = _group_aggregates(conn, metric, col)
            ssw = sum((g_n - 1) * v for _, g_n, _, v, _ in groups if g_n > 1)
            reduction = 1 - ssw / ((n - 1) * var_total) if n > 1 else 0.0
            if best is None or reduction > best[1]:
                best = (col, reduction, [(g, g_n, m) for g, g_n, m, _, _ in groups])
        if best is None:
            raise ValueError("no category columns to segment by")
        col, reduction, groups = best
        rules = [
            f"{col}={g} → {m / overall:.1f}× overall {metric}"
            for g, g_n, m in groups
            if g_n / n >= 0.1 and overall and abs(m / overall - 1) >= 0.15
        ][:4]
        slots = {
            "metric": metric,
            "segment_column": col,
            "variance_explained_pct": round(reduction * 100, 1),
            "rules": rules,
        }
        summary = "; ".join(rules) if rules else "no segment deviates more than 15% from overall"
        return [
            make_finding(
                "segment",
                slots,
                (
                    f"'{col}' explains {slots['variance_explained_pct']:g}% of the variation "
                    f"in {metric}: {summary}"
                ),
                effect_size=reduction,
                n=n,
                objective=objective,
            )
        ]
    finally:
        conn.close()
