"""Analysis Engine Phase 5 — the chart-chooser grammar.

A deterministic scoring grammar (measure count × dimension type × cardinality
× temporality) decides which EXTRA visual kinds a dataset earns beyond the
base picks (stats / line / bar / pie / table — whose own guards already cap
categories at 12 + Other and allow one pie). The chooser only *suggests*
recipe specs; the builders validate against the real data and drop what
doesn't fit, so a suggestion can never fabricate a chart.

Shared by BOTH build paths (DuckDB engine and the stdlib profiler) so the
same file earns the same visuals everywhere — part of the semantic-layer
guarantee.

Complexity: O(columns); no data access.
"""

from __future__ import annotations

from typing import Any

_MIN_SCATTER_ROWS = 30  # a scatter over fewer points reads as noise
_RADAR_MIN_METRICS = 3
_RADAR_MAX_METRICS = 6
_MAX_SUGGESTIONS = 2


def suggest_extras(columns: list[dict[str, str]], rows: int, existing: int) -> list[dict[str, Any]]:
    """Recipe specs for extra visual kinds this data shape earns.

    columns: [{name, kind}] with kind ∈ number/date/category/id/text —
    the neutral profile both build paths already produce."""
    numbers = [c["name"] for c in columns if c["kind"] == "number"]
    categories = [c["name"] for c in columns if c["kind"] == "category"]
    dates = [c["name"] for c in columns if c["kind"] == "date"]
    scored: list[tuple[float, dict[str, Any]]] = []

    # Radar — a multi-metric profile across a small category: needs 3–6
    # measures and a compact dimension (the builder checks 2–6 groups).
    if len(numbers) >= _RADAR_MIN_METRICS and categories:
        scored.append(
            (
                0.8,
                {
                    "kind": "radar",
                    "by": categories[0],
                    "columns": numbers[:_RADAR_MAX_METRICS],
                },
            )
        )
    # Scatter — two measures and enough rows; temporality favors the line
    # instead, so a dated table scores lower.
    if len(numbers) >= 2 and rows >= _MIN_SCATTER_ROWS:
        scored.append(
            (
                0.6 + (0.1 if not dates else 0.0),
                {"kind": "scatter", "by": numbers[0], "metric": numbers[1]},
            )
        )
    # Histogram — a lone measure with nothing to group by still deserves a
    # distribution view.
    if len(numbers) >= 1 and not categories and not dates and rows >= 20:
        scored.append((0.5, {"kind": "histogram", "metric": numbers[0]}))

    scored.sort(key=lambda s: -s[0])
    room = max(0, _MAX_SUGGESTIONS - max(0, existing - 6))
    return [spec for _, spec in scored[:room]]
