"""Analysis Engine Phase 3 — statistical primitives, dependency-free.

Hand-rolled special functions (regularized incomplete beta / gamma via the
standard Numerical-Recipes series + continued fractions, on math.lgamma) give
exact-enough p-values for t / F / chi-square without adding scipy to the
frozen stack. Accuracy is pinned by tests against known table values
(t=2.0,df=10 → p≈0.0734; chi²=3.841,df=1 → p≈0.05; z=1.96 → p≈0.05).

Everything here is a pure function over numbers already aggregated by DuckDB —
no data access, no side effects.

Complexity: all O(1) on aggregates except Holm (O(m log m) over m tests).
"""

from __future__ import annotations

import math

_EPS = 3e-12
_FPMIN = 1e-300
_MAXIT = 500


# ---------------------------------------------------------------------------
# Special functions
# ---------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = _FPMIN if abs(d) < _FPMIN else d
    d = 1.0 / d
    h = d
    for m in range(1, _MAXIT + 1):
        m2 = 2 * m
        for aa in (
            m * (b - m) * x / ((qam + m2) * (a + m2)),
            -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2)),
        ):
            d = 1.0 + aa * d
            d = _FPMIN if abs(d) < _FPMIN else d
            c = 1.0 + aa / c
            c = _FPMIN if abs(c) < _FPMIN else c
            d = 1.0 / d
            h *= d * c
        if abs(d * c - 1.0) < _EPS:
            break
    return h


def betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    bt = math.exp(ln)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _gser(a: float, x: float) -> float:
    ap, total = a, 1.0 / a
    delt = total
    for _ in range(_MAXIT):
        ap += 1.0
        delt *= x / ap
        total += delt
        if abs(delt) < abs(total) * _EPS:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gcf(a: float, x: float) -> float:
    b, c, d = x + 1.0 - a, 1.0 / _FPMIN, 1.0 / (x + 1.0 - a)
    h = d
    for i in range(1, _MAXIT):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = _FPMIN if abs(d) < _FPMIN else d
        c = b + an / c
        c = _FPMIN if abs(c) < _FPMIN else c
        d = 1.0 / d
        delt = d * c
        h *= delt
        if abs(delt - 1.0) < _EPS:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def gammq(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x) — the chi-square survival fn."""
    if x <= 0.0 or a <= 0.0:
        return 1.0
    if x < a + 1.0:
        return 1.0 - _gser(a, x)
    return _gcf(a, x)


# ---------------------------------------------------------------------------
# Survival functions (two-sided p-values)
# ---------------------------------------------------------------------------


def normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def t_p_two_sided(t: float, df: float) -> float:
    if df <= 0:
        return 1.0
    return betai(df / 2.0, 0.5, df / (df + t * t))


def f_sf(f: float, df1: float, df2: float) -> float:
    if f <= 0 or df1 <= 0 or df2 <= 0:
        return 1.0
    return betai(df2 / 2.0, df1 / 2.0, df2 / (df2 + df1 * f))


def chi2_sf(x: float, df: float) -> float:
    return gammq(df / 2.0, x / 2.0)


# ---------------------------------------------------------------------------
# Tests from group aggregates (n, mean, variance come from DuckDB)
# ---------------------------------------------------------------------------


def welch_t(
    n1: int, mean1: float, var1: float, n2: int, mean2: float, var2: float
) -> tuple[float, float, float]:
    """(t, df, p) — Welch's unequal-variance t-test from aggregates."""
    if n1 < 2 or n2 < 2:
        return 0.0, 0.0, 1.0
    se2 = var1 / n1 + var2 / n2
    if se2 <= 0:
        return 0.0, 0.0, 1.0
    t = (mean1 - mean2) / math.sqrt(se2)
    df = se2 * se2 / ((var1 / n1) ** 2 / (n1 - 1) + (var2 / n2) ** 2 / (n2 - 1) or _FPMIN)
    return t, df, t_p_two_sided(t, df)


def cohens_d(n1: int, mean1: float, var1: float, n2: int, mean2: float, var2: float) -> float:
    pooled = ((n1 - 1) * var1 + (n2 - 1) * var2) / max(1, n1 + n2 - 2)
    if pooled <= 0:
        return 0.0
    return (mean1 - mean2) / math.sqrt(pooled)


def mann_whitney(n1: int, n2: int, rank_sum1: float, tie_term: float) -> tuple[float, float, float]:
    """(U, rank_biserial, p) from rank aggregates. tie_term = Σ(t³−t) over tie
    groups; the normal approximation with tie correction + continuity."""
    if n1 < 1 or n2 < 1:
        return 0.0, 0.0, 1.0
    n = n1 + n2
    u1 = rank_sum1 - n1 * (n1 + 1) / 2.0
    mu = n1 * n2 / 2.0
    var = n1 * n2 / 12.0 * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    if var <= 0:
        return u1, 0.0, 1.0
    z = (abs(u1 - mu) - 0.5) / math.sqrt(var)
    r = 1.0 - 2.0 * u1 / (n1 * n2)  # rank-biserial effect size
    return u1, r, 2.0 * normal_sf(max(0.0, z))


def anova_oneway(groups: list[tuple[int, float, float]]) -> tuple[float, float, float, float]:
    """(F, df_between, df_within, p) from per-group (n, mean, variance)."""
    groups = [g for g in groups if g[0] >= 2]
    k = len(groups)
    n = sum(g[0] for g in groups)
    if k < 2 or n <= k:
        return 0.0, 0.0, 0.0, 1.0
    grand = sum(g[0] * g[1] for g in groups) / n
    ssb = sum(g[0] * (g[1] - grand) ** 2 for g in groups)
    ssw = sum((g[0] - 1) * g[2] for g in groups)
    df_b, df_w = k - 1, n - k
    if ssw <= 0:
        return math.inf, df_b, df_w, 0.0
    f = (ssb / df_b) / (ssw / df_w)
    return f, df_b, df_w, f_sf(f, df_b, df_w)


def eta_squared(groups: list[tuple[int, float, float]]) -> float:
    n = sum(g[0] for g in groups)
    if n == 0:
        return 0.0
    grand = sum(g[0] * g[1] for g in groups) / n
    ssb = sum(g[0] * (g[1] - grand) ** 2 for g in groups)
    ssw = sum((max(g[0], 1) - 1) * g[2] for g in groups)
    total = ssb + ssw
    return ssb / total if total > 0 else 0.0


def chi2_independence(table: list[list[float]]) -> tuple[float, float, float, float]:
    """(chi2, df, p, cramers_v) from a contingency table of counts."""
    rows, cols = len(table), len(table[0]) if table else 0
    if rows < 2 or cols < 2:
        return 0.0, 0.0, 1.0, 0.0
    row_sums = [sum(r) for r in table]
    col_sums = [sum(table[i][j] for i in range(rows)) for j in range(cols)]
    n = sum(row_sums)
    if n <= 0:
        return 0.0, 0.0, 1.0, 0.0
    chi2 = 0.0
    for i in range(rows):
        for j in range(cols):
            expected = row_sums[i] * col_sums[j] / n
            if expected > 0:
                chi2 += (table[i][j] - expected) ** 2 / expected
    df = (rows - 1) * (cols - 1)
    v = math.sqrt(chi2 / (n * max(1, min(rows - 1, cols - 1))))
    return chi2, float(df), chi2_sf(chi2, df), v


def corr_p(r: float, n: int) -> float:
    """Two-sided p for a Pearson/Spearman r via the t transform."""
    if n < 3 or abs(r) >= 1.0:
        return 0.0 if abs(r) >= 1.0 and n >= 3 else 1.0
    t = r * math.sqrt((n - 2) / (1.0 - r * r))
    return t_p_two_sided(abs(t), n - 2)


def holm(p_values: list[float]) -> list[float]:
    """Holm-Bonferroni step-down adjustment; monotone, capped at 1."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * p_values[i])
        adjusted[i] = min(1.0, running)
    return adjusted
