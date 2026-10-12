"""Analysis Engine Phase 5 — chooser grammar, new chart kinds, semantic
parity, the Word report, the insight deck, and explain-this-chart.

Spec done-when: the same metric renders identically in chat, Grafana, and
Power BI (they all consume ONE spec list), and the Word report reproduces
the dashboard's numbers exactly.
"""

from __future__ import annotations

import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from sila.services.analysis import chooser, engine, explain, semantic
from sila.services.analysis.report import build_report_sections
from sila.services.connectors.grafana_dashboard import _chart_csv, _panel_type_for
from sila.services.connectors.office_files import build_docx_sections
from sila.services.connectors.powerbi_report import pick_visuals
from sila.services.sila.data_dashboard import build_data_dashboard, parse_csv, profile_columns

FIXTURES = Path(__file__).parent / "fixtures" / "analysis"
GOLDEN = FIXTURES / "golden_sales.csv"
WIDE = FIXTURES / "wide_sales.csv"
LONE = FIXTURES / "lone_metric.csv"


def _ctx() -> Any:
    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext

    return ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )


# ---------------------------------------------------------------------------
# Chooser grammar + the new kinds, identical on both build paths
# ---------------------------------------------------------------------------


def test_chooser_grammar_earns_radar_scatter_and_histogram() -> None:
    wide_profile = [
        {"name": "region", "kind": "category"},
        {"name": "sales", "kind": "number"},
        {"name": "profit", "kind": "number"},
        {"name": "units", "kind": "number"},
    ]
    kinds = [s["kind"] for s in chooser.suggest_extras(wide_profile, 40, 5)]
    assert kinds == ["radar", "scatter"]
    lone = [{"name": "value", "kind": "number"}]
    assert [s["kind"] for s in chooser.suggest_extras(lone, 24, 2)] == ["scatter", "histogram"][
        1:
    ]  # one number → no scatter, histogram only


def test_new_kinds_render_identically_on_both_build_paths() -> None:
    """The chooser + builders are shared: DuckDB and stdlib must emit the
    SAME radar/scatter/histogram charts for the same file."""
    text = WIDE.read_text()
    stdlib_spec = build_data_dashboard("wide_sales.csv", text)
    duck_spec = engine.dashboard_from_dataset(str(WIDE), "wide_sales.csv")
    for kind in ("radar", "scatter"):
        std = next(c for c in stdlib_spec["charts"] if c["kind"] == kind)
        duck = next(c for c in duck_spec["charts"] if c["kind"] == kind)
        assert std == duck, f"{kind} differs between build paths"
    lone_std = build_data_dashboard("lone_metric.csv", LONE.read_text())
    lone_duck = engine.dashboard_from_dataset(str(LONE), "lone_metric.csv")
    std_h = next(c for c in lone_std["charts"] if c["kind"] == "histogram")
    duck_h = next(c for c in lone_duck["charts"] if c["kind"] == "histogram")
    assert std_h == duck_h


def test_radar_is_normalized_to_100() -> None:
    spec = build_data_dashboard("wide_sales.csv", WIDE.read_text())
    radar = next(c for c in spec["charts"] if c["kind"] == "radar")
    assert len(radar["series"]) >= 2 and len(radar["data"]) >= 3
    for row in radar["data"]:
        peak = max(v for k, v in row.items() if k != "name")
        assert peak == 100.0  # each metric's best group anchors the scale


# ---------------------------------------------------------------------------
# Done-when 1 — one spec, three surfaces
# ---------------------------------------------------------------------------


def test_same_metric_renders_identically_across_surfaces() -> None:
    spec = build_data_dashboard("golden_sales.csv", GOLDEN.read_text())
    stat = next(c for c in spec["charts"] if c["kind"] == "stat")
    # Chat surface: the stat card value.
    chat_value = stat["value"]
    # Grafana surface: the SAME chart spec serialized for the stat panel.
    assert _chart_csv(stat) == f"value\n{chat_value}"
    assert _panel_type_for(stat) == "stat"
    # Power BI surface: the SAME recipe list drives the visuals (authoritative
    # mirror) — the stat metric becomes a card bound to the same column+agg.
    header, rows = parse_csv(GOLDEN.read_text())
    cols = profile_columns(header, rows)
    visuals = pick_visuals(cols, spec["charts"], authoritative=True)
    blobs = [str(v) for v in visuals]
    assert any(stat["metric"] in b and "card" in b.lower() for b in blobs)
    # And the semantic layer records ONE meaning for it.
    layer = semantic.layer_from_charts(spec["charts"])
    assert {"metric": "revenue", "agg": "avg", "dimensions": []} in layer or any(
        entry["metric"] == "revenue" and entry["agg"] == "avg" for entry in layer
    )


def test_new_kinds_have_grafana_mappings() -> None:
    assert _panel_type_for({"kind": "scatter"}) == "xychart"
    assert _panel_type_for({"kind": "histogram"}) == "barchart"
    assert _panel_type_for({"kind": "radar"}) == "barchart"
    csv = _chart_csv({"kind": "scatter", "data": [{"x": 1, "y": 2}]})
    assert csv == "x,y\n1,2"
    radar_csv = _chart_csv(
        {"kind": "radar", "series": ["A", "B"], "data": [{"name": "sales", "A": 100, "B": 80}]}
    )
    assert radar_csv == "name,A,B\nsales,100,80"


def test_pbix_skips_derived_kinds_without_breaking_the_mirror() -> None:
    header, rows = parse_csv(GOLDEN.read_text())
    cols = profile_columns(header, rows)
    recipes = [
        {"kind": "stat", "metric": "revenue"},
        {"kind": "radar", "by": "region", "columns": ["revenue", "units"]},
        {"kind": "histogram", "metric": "revenue"},
    ]
    visuals = pick_visuals(cols, recipes, authoritative=True)
    assert visuals  # the stat card survives; derived kinds are skipped quietly


# ---------------------------------------------------------------------------
# Done-when 2 — the Word report reproduces the dashboard's numbers exactly
# ---------------------------------------------------------------------------


async def test_report_reproduces_dashboard_numbers_exactly() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    ctx.build_state["uploaded_files"] = [
        {"name": "golden_sales.csv", "content": GOLDEN.read_text()}
    ]
    res = await execute_tool("create_data_dashboard", ctx, {})
    assert res.ui is not None
    stats = ctx.build_state["dashboard_stats"]
    assert stats and all(s["value"] is not None for s in stats)
    sections = build_report_sections(ctx.build_state)
    figures = next(s for s in sections if s["heading"] == "Dashboard figures")["body"]
    for card in stats:
        assert str(card["value"]) in figures  # exact, not re-computed
    # …and the numbers survive into the actual .docx bytes.
    doc = build_docx_sections("Report", [(s["heading"], s["body"]) for s in sections])
    with zipfile.ZipFile(BytesIO(doc)) as z:
        xml = z.read("word/document.xml").decode()
    for card in stats:
        assert str(card["value"]) in xml


def test_report_appendix_carries_recipe_and_semantic_layer() -> None:
    state = {
        "cleaning_recipe": {"sales.csv": [{"op": "dedupe"}]},
        "semantic_layer": [{"metric": "revenue", "agg": "avg", "dimensions": ["region"]}],
    }
    appendix = next(s for s in build_report_sections(state) if s["heading"] == "Appendix")["body"]
    assert "remove exact duplicate rows" in appendix
    assert "revenue = avg over region" in appendix


# ---------------------------------------------------------------------------
# Explain this chart — bilingual, guarded
# ---------------------------------------------------------------------------


def test_explain_chart_bilingual_and_guarded() -> None:
    bar = {
        "kind": "bar",
        "title": "revenue by region",
        "metric": "revenue",
        "by": "region",
        "agg": "sum",
        "data": [{"name": "Muscat", "value": 9100}, {"name": "Sohar", "value": 3180}],
    }
    text = explain.explain_chart(bar)
    assert "Muscat" in text["en"] and "9100" in text["en"]
    assert "Muscat" in text["ar"] and "revenue" in text["ar"]


async def test_explain_tool_exact_title_match() -> None:
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx()
    ctx.build_state["uploaded_files"] = [
        {"name": "golden_sales.csv", "content": GOLDEN.read_text()}
    ]
    await execute_tool("create_data_dashboard", ctx, {})
    res = await execute_tool("explain_chart", ctx, {"title": "revenue by region"})
    assert "EN:" in res.content and "AR:" in res.content
    missing = await execute_tool("explain_chart", ctx, {"title": "no such chart"})
    assert "The dashboard has:" in missing.content


# ---------------------------------------------------------------------------
# The insight deck
# ---------------------------------------------------------------------------


async def test_insight_deck_requires_insights_then_delegates(monkeypatch: Any) -> None:
    import sila.services.sila.tool_registry as tr

    ctx = _ctx()
    res = await tr.execute_tool("create_insight_deck", ctx, {})
    assert "rank_insights" in res.content

    captured: dict[str, Any] = {}

    async def _fake_deck(_ctx: Any, args: dict[str, Any]) -> Any:
        captured.update(args)
        return tr.ToolResult(content="deck built", summary="ok")

    monkeypatch.setattr(tr, "_create_slide_deck", _fake_deck)
    ctx.build_state["insights"] = [
        {
            "kind": "difference",
            "statement_slots": {"metric": "revenue", "group_high": "Muscat"},
            "what_happened": "Muscat averages 1262.5 revenue vs Sohar's 795",
            "so_what": "The gap is the lever.",
            "confidence": "medium",
            "caveats": ["n=24 is small"],
        }
    ]
    ctx.build_state["exec_summary"] = {"bullets": ["Muscat leads revenue."]}
    # No style yet → the tool refuses and demands the audience question first.
    ask = await tr.execute_tool("create_insight_deck", ctx, {})
    assert "ask the user" in ask.content and "'executive'" in ask.content
    assert not captured
    res = await tr.execute_tool("create_insight_deck", ctx, {"style": "executive"})
    assert res.content == "deck built"
    assert captured["slides"][0]["title"] == "Executive summary"
    slide = captured["slides"][1]
    assert slide["title"] == "Muscat leads on revenue"  # headline, never truncated
    assert "…" not in slide["title"]
    assert slide["bullets"][0].startswith("Muscat averages 1262.5")  # full sentence
    assert any(b.startswith("Note:") for b in slide["bullets"])
    assert not any(b.startswith("Confidence:") for b in slide["bullets"])  # exec style


def test_deck_styles_and_slide_titles() -> None:
    from sila.services.analysis.report import deck_slides, slide_title

    assert slide_title("trend", {"metric": "revenue", "slope_per_day": -24.3}) == (
        "revenue is falling"
    )
    assert slide_title("trend", {"metric": "revenue", "changepoint_date": "2026-01-14"}) == (
        "A shift in revenue"
    )
    assert slide_title("anomaly", {"metric": "revenue"}) == "Unusual days in revenue"
    card = {
        "kind": "trend",
        "statement_slots": {"metric": "revenue", "slope_per_day": -24.3},
        "what_happened": "revenue is falling by -24.3/day over 24 days",
        "so_what": "Confirm the operational cause first.",
        "confidence": "low",
        "caveats": ["n=24 is too small to claim statistical significance"],
    }
    technical = deck_slides([card], None, "technical")[0]
    assert any(b.startswith("Confidence: low") for b in technical["bullets"])
    assert any(b.startswith("Caveat:") for b in technical["bullets"])
    executive = deck_slides([card], None, "executive")[0]
    assert any(b.startswith("Note:") for b in executive["bullets"])
    assert not any(b.startswith("Confidence:") for b in executive["bullets"])


def test_parse_csv_max_rows_lifts_the_pbix_cap() -> None:
    text = "a,b\n" + "\n".join(f"{i},{i}" for i in range(7000))
    _, capped = parse_csv(text)
    assert len(capped) == 5000  # the chat default is unchanged
    _, full = parse_csv(text, max_rows=1_000_000)
    assert len(full) == 7000  # Power BI aggregates the SAME rows as chat
