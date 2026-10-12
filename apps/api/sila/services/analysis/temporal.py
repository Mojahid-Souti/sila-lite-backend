"""Analysis Engine Phase 3 — temporal analysis: trend, changepoint, anomaly,
and a baseline forecast that refuses without enough history.

The series is aggregated to daily points by DuckDB (full file), then the
O(points) math runs in Python over ≤ ~1000 points. Findings only — no prose,
no raw rows to the model.

Complexity: O(rows) SQL aggregation; O(points²) worst case for the binary-
segmentation changepoint over ≤ 1000 points (bounded, background-safe).
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from sila.services.analysis import stats
from sila.services.analysis.engine import _connect, _q
from sila.services.analysis.findings import Finding, make_finding

_MAX_POINTS = 1000
_MIN_SEGMENT = 5
_SHIFT_Z = 3.0  # mean-shift z to call a changepoint
_ANOMALY_Z = 3.5  # robust z (median/MAD) to flag a point
_SEASON_LAG = 7  # weekly seasonality on daily data
_SEASON_ACF = 0.3
_FORECAST_H = 7


def daily_series(path: str, metric: str, date_col: str) -> list[tuple[str, float]]:
    conn = _connect(path)
    try:
        rows = conn.execute(
            f"SELECT CAST(date_trunc('day', {_q(date_col)}) AS DATE) AS d, avg({_q(metric)}) "
            f"FROM dataset WHERE {_q(date_col)} IS NOT NULL AND {_q(metric)} IS NOT NULL "
            f"GROUP BY 1 ORDER BY 1 LIMIT {_MAX_POINTS}"
        ).fetchall()
        return [(str(d), float(v)) for d, v in rows if v is not None]
    finally:
        conn.close()


def _ols(y: list[float]) -> tuple[float, float, float]:
    """(slope, stderr, p) of y over its index — trend per day."""
    n = len(y)
    if n < 3:
        return 0.0, 0.0, 1.0
    xs = range(n)
    mx, my = (n - 1) / 2.0, sum(y) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (v - my) for x, v in zip(xs, y, strict=True))
    slope = sxy / sxx
    residuals = [v - (my + slope * (x - mx)) for x, v in zip(xs, y, strict=True)]
    dof = n - 2
    s2 = sum(r * r for r in residuals) / dof if dof > 0 else 0.0
    se = math.sqrt(s2 / sxx) if sxx > 0 else 0.0
    t = slope / se if se > 0 else 0.0
    return slope, se, stats.t_p_two_sided(abs(t), dof) if se > 0 else 1.0


def _changepoint(y: list[float]) -> tuple[int, float] | None:
    """Binary segmentation: the split minimizing pooled SSE, kept only when
    the mean shift is ≥ _SHIFT_Z pooled standard errors."""
    n = len(y)
    if n < 2 * _MIN_SEGMENT:
        return None
    best_i, best_sse = -1, math.inf
    prefix = [0.0]
    prefix_sq = [0.0]
    for v in y:
        prefix.append(prefix[-1] + v)
        prefix_sq.append(prefix_sq[-1] + v * v)

    def sse(lo: int, hi: int) -> float:  # [lo, hi)
        count = hi - lo
        total = prefix[hi] - prefix[lo]
        return (prefix_sq[hi] - prefix_sq[lo]) - total * total / count

    for i in range(_MIN_SEGMENT, n - _MIN_SEGMENT + 1):
        split_sse = sse(0, i) + sse(i, n)
        if split_sse < best_sse:
            best_i, best_sse = i, split_sse
    left, right = y[:best_i], y[best_i:]
    m1, m2 = statistics.fmean(left), statistics.fmean(right)
    var1 = statistics.pvariance(left) if len(left) > 1 else 0.0
    var2 = statistics.pvariance(right) if len(right) > 1 else 0.0
    pooled = math.sqrt(var1 / len(left) + var2 / len(right))
    if pooled <= 0:
        return (best_i, math.inf) if m1 != m2 else None
    z = abs(m1 - m2) / pooled
    return (best_i, z) if z >= _SHIFT_Z else None


def _anomalies(points: list[tuple[str, float]]) -> list[dict[str, Any]]:
    """Trailing median/MAD robust z over a 7-point window; flagged, capped."""
    out: list[dict[str, Any]] = []
    window = _SEASON_LAG
    for i in range(window, len(points)):
        prev = [v for _, v in points[i - window : i]]
        med = statistics.median(prev)
        mad = statistics.median(abs(v - med) for v in prev)
        if mad <= 0:
            continue
        z = 0.6745 * (points[i][1] - med) / mad
        if abs(z) >= _ANOMALY_Z:
            out.append(
                {"date": points[i][0], "value": round(points[i][1], 3), "robust_z": round(z, 1)}
            )
    return out[:5]


def _autocorr(y: list[float], lag: int) -> float:
    n = len(y)
    if n <= lag + 2:
        return 0.0
    mean = statistics.fmean(y)
    denom = sum((v - mean) ** 2 for v in y)
    if denom <= 0:
        return 0.0
    return sum((y[i] - mean) * (y[i + lag] - mean) for i in range(n - lag)) / denom


def trend_findings(
    path: str, metric: str, date_col: str, objective: dict[str, Any] | None = None
) -> list[Finding]:
    """Trend + slope CI, changepoint, anomalies — each a typed Finding with an
    evidence line chart in the existing spec format."""
    points = daily_series(path, metric, date_col)
    if len(points) < 3:
        raise ValueError(f"'{date_col}' yields fewer than 3 daily points — no trend to fit")
    y = [v for _, v in points]
    n = len(y)
    evidence = {
        "kind": "line",
        "title": f"{metric} over {date_col}",
        "series": [metric],
        "data": [{"x": d, metric: round(v, 2)} for d, v in points],
        "by": date_col,
        "series_cols": [metric],
    }
    slope, se, p = _ols(y)
    ci = 1.96 * se
    direction = "rising" if slope > 0 else "falling" if slope < 0 else "flat"
    change_pct = round((y[-1] - y[0]) / abs(y[0]) * 100, 1) if y[0] else None
    out = [
        make_finding(
            "trend",
            {
                "metric": metric,
                "date_column": date_col,
                "days": n,
                "slope_per_day": round(slope, 4),
                "slope_ci95": [round(slope - ci, 4), round(slope + ci, 4)],
                "change_over_window_pct": change_pct,
            },
            (
                f"{metric} is {direction} by {slope:+.3g}/day over {n} days "
                f"(95% CI {slope - ci:+.3g} to {slope + ci:+.3g})"
                + (f", {change_pct:+g}% across the window" if change_pct is not None else "")
            ),
            effect_size=abs(slope),
            p_value=p,
            p_adjusted=p,
            n=n,
            evidence=evidence,
            objective=objective,
        )
    ]
    cp = _changepoint(y)
    if cp is not None:
        idx, z = cp
        before = statistics.fmean(y[:idx])
        after = statistics.fmean(y[idx:])
        out.append(
            make_finding(
                "trend",
                {
                    "metric": metric,
                    "changepoint_date": points[idx][0],
                    "mean_before": round(before, 3),
                    "mean_after": round(after, 3),
                    "shift_z": None if math.isinf(z) else round(z, 1),
                },
                (
                    f"{metric} shifted around {points[idx][0]}: it averaged "
                    f"{before:g} before and {after:g} after"
                ),
                effect_size=abs(after - before),
                n=n,
                evidence=evidence,
                objective=objective,
            )
        )
    anomalies = _anomalies(points)
    if anomalies:
        listed = "; ".join(f"{a['date']} ({a['value']:g}, z={a['robust_z']:g})" for a in anomalies)
        out.append(
            make_finding(
                "anomaly",
                {"metric": metric, "count": len(anomalies), "anomalies": anomalies},
                f"{len(anomalies)} unusual day(s) in {metric}: {listed}",
                effect_size=max(abs(a["robust_z"]) for a in anomalies),
                n=n,
                caveats=["anomalies are flagged for review, not removed from the data"],
                evidence=evidence,
                objective=objective,
            )
        )
    return out


def forecast_finding(
    path: str, metric: str, date_col: str, objective: dict[str, Any] | None = None
) -> Finding:
    """Seasonal-naive (weekly) or drift baseline with an honest interval.
    REFUSES (raises ValueError) without ≥ 2 seasonal cycles / 10 points —
    a refusal the model must relay, never paper over."""
    points = daily_series(path, metric, date_col)
    y = [v for _, v in points]
    n = len(y)
    seasonal = n >= 3 * _SEASON_LAG and _autocorr(y, _SEASON_LAG) >= _SEASON_ACF
    if seasonal:
        residuals = [y[i] - y[i - _SEASON_LAG] for i in range(_SEASON_LAG, n)]
        forecast = [round(v, 3) for v in y[-_SEASON_LAG:]]
        method = "seasonal-naive (weekly)"
    else:
        if n < 2 * _SEASON_LAG:
            raise ValueError(
                f"only {n} daily points — a forecast needs at least two full cycles "
                f"({2 * _SEASON_LAG} days); refusing rather than guessing"
            )
        slope, _, _ = _ols(y)
        residuals = [y[i] - y[i - 1] - slope for i in range(1, n)]
        forecast = [round(y[-1] + slope * h, 3) for h in range(1, _FORECAST_H + 1)]
        method = "drift baseline"
    sd = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0
    interval = round(1.96 * sd, 3)
    return make_finding(
        "trend",
        {
            "metric": metric,
            "method": method,
            "horizon_days": _FORECAST_H,
            "forecast": forecast,
            "interval_plus_minus": interval,
        },
        (
            f"next {_FORECAST_H} days of {metric} ({method}): "
            f"{', '.join(f'{v:g}' for v in forecast)} — each ±{interval:g} (95%)"
        ),
        effect_size=0.0,
        n=n,
        caveats=["a baseline forecast, not a model — treat as a rough guide"],
        objective=objective,
    )
