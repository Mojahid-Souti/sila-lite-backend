"""Analysis Engine Phase 5 — the semantic layer: one metric, one meaning.

The chart recipes (metric / agg / by) that every renderer consumes are
extracted here into a MetricRegistry: the single description of what each
number MEANS, shared by chat, Grafana, and Power BI. The parity guarantee —
"Grafana says 4,182, Power BI says 4,190" cannot happen — holds because all
three surfaces render the SAME spec list; this module makes that contract
inspectable (and puts it in the report appendix).

Complexity: O(charts); no data access.
"""

from __future__ import annotations

from typing import Any


def layer_from_charts(charts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MetricRegistry from a rendered chart list: one entry per (metric, agg)
    with every dimension it is broken down by. Deterministic order."""
    registry: dict[tuple[str, str], set[str]] = {}
    for chart in charts:
        if not isinstance(chart, dict):
            continue
        metric = str(chart.get("metric") or "").strip()
        if not metric:
            continue
        agg = str(chart.get("agg") or "avg")
        dims = registry.setdefault((metric, agg), set())
        by = str(chart.get("by") or "").strip()
        if by:
            dims.add(by)
    return [
        {"metric": m, "agg": a, "dimensions": sorted(dims)}
        for (m, a), dims in sorted(registry.items())
    ]


def describe_layer(layer: list[dict[str, Any]]) -> list[str]:
    """Plain-language lines for the report appendix."""
    lines = []
    for entry in layer:
        dims = ", ".join(entry["dimensions"]) if entry["dimensions"] else "the whole file"
        lines.append(f"{entry['metric']} = {entry['agg']} over {dims}")
    return lines
