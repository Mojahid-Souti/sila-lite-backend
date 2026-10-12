"""CSV → in-Sila dashboard: deterministic profiling + chart selection."""

from __future__ import annotations

import uuid

import pytest

from sila.services.sila.data_dashboard import (
    build_data_dashboard,
    parse_csv,
    profile_columns,
)

_SALES = (
    "date,region,revenue,units\n"
    "2026-01-01,North,1000,10\n"
    "2026-01-02,South,2000,20\n"
    "2026-01-03,North,1500,15\n"
    "2026-01-04,East,500,5\n"
)


def test_parse_and_profile_types() -> None:
    header, rows = parse_csv(_SALES)
    assert header == ["date", "region", "revenue", "units"]
    kinds = {c.name: c.kind for c in profile_columns(header, rows)}
    assert kinds == {"date": "date", "region": "category", "revenue": "number", "units": "number"}


def test_dashboard_has_stats_line_and_bar() -> None:
    dash = build_data_dashboard("sales_2026.csv", _SALES)
    kinds = [c["kind"] for c in dash["charts"]]
    assert "stat" in kinds and "line" in kinds and "bar" in kinds
    assert dash["title"] == "Sales 2026"
    assert dash["rows"] == 4
    line = next(c for c in dash["charts"] if c["kind"] == "line")
    assert line["data"][0]["x"] == "2026-01-01"  # sorted by date
    bar = next(c for c in dash["charts"] if c["kind"] == "bar")
    by_name = {d["name"]: d["value"] for d in bar["data"]}
    assert by_name["North"] == 2500.0  # aggregated across rows


def test_focus_prioritises_the_named_column() -> None:
    dash = build_data_dashboard("s.csv", _SALES, focus="units")
    stat_titles = [c["title"] for c in dash["charts"] if c["kind"] == "stat"]
    assert stat_titles[0] == "units (avg)"


def test_semicolon_delimiter_sniffed() -> None:
    text = "name;score\nA;1\nB;2\n"
    header, rows = parse_csv(text)
    assert header == ["name", "score"] and len(rows) == 2


def test_unusable_input_raises() -> None:
    with pytest.raises(ValueError):
        parse_csv("just one line no data")
    with pytest.raises(ValueError):
        build_data_dashboard("x.csv", "a,b\n,\n")  # nothing chartable


def test_category_cap_groups_other() -> None:
    # 20 distinct values: still a category (≤24 uniques) but past the 12-bar cap,
    # so the tail is grouped into "Other".
    rows = "\n".join(f"cat{i},1" for i in range(20))
    dash = build_data_dashboard("t.csv", f"kind,n\n{rows}\n")
    bar = next(c for c in dash["charts"] if c["kind"] == "bar")
    assert len(bar["data"]) == 13  # 12 + "Other"
    assert bar["data"][-1]["name"] == "Other"


async def test_tool_reads_the_stash_and_emits_the_dashboard() -> None:
    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext, execute_tool

    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )
    # No file attached → a clear ask, no crash.
    empty = await execute_tool("create_data_dashboard", ctx, {})
    assert "No data file" in empty.content

    ctx.build_state["uploaded_files"] = [{"name": "sales.csv", "content": _SALES}]
    res = await execute_tool("create_data_dashboard", ctx, {"focus": "revenue"})
    assert res.ui is not None and res.ui["command"] == "open_dashboard"
    dash = res.ui["dashboard"]
    assert dash["source"] == "sales.csv" and len(dash["charts"]) >= 3
    assert "sales.csv" in res.content


async def test_tool_charts_data_the_model_researched_itself() -> None:
    """A "search the web, then chart it here" turn has no attachment. Without an
    inline path the tool used to hand the user a CSV and ask them to upload it
    back — a round trip through the user for data already in hand."""
    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext, execute_tool

    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )
    res = await execute_tool(
        "create_data_dashboard",
        ctx,
        {"csv_data": _SALES, "title": "Renewable Generation 2026"},
    )
    assert res.ui is not None and res.ui["command"] == "open_dashboard"
    dash = res.ui["dashboard"]
    assert len(dash["charts"]) >= 3
    # The title travels through the synthetic file name.
    assert dash["source"] == "renewable-generation-2026.csv"
    assert "No data file" not in res.content


def test_attachment_stash_parses_blocks() -> None:
    from sila.services.sila.orchestrator.runner import stash_attachments

    state: dict = {}
    msg = "build a dashboard\n[Attached file: sales.csv]\na,b\n1,2\n[End of sales.csv]"
    stash_attachments(msg, state)
    assert state["uploaded_files"] == [{"name": "sales.csv", "content": "a,b\n1,2"}]
    # A later message WITHOUT attachments keeps the stash (follow-up rebuilds).
    stash_attachments("make it focus on b", state)
    assert state["uploaded_files"][0]["name"] == "sales.csv"


def test_unnamed_index_column_is_dropped_and_stats_are_averages() -> None:
    # A pandas-style export: unnamed first column counting 0..n-1.
    text = ",name,roe\n0,A,0.2\n1,B,0.4\n2,C,0.6\n"
    dash = build_data_dashboard("x.csv", text)
    titles = [c["title"] for c in dash["charts"]]
    assert not any("col1" in t for t in titles)  # the index never charts
    stat = next(c for c in dash["charts"] if c["kind"] == "stat")
    assert stat["title"] == "roe (avg)" and stat["value"] == 0.4  # avg, not sum
    assert "min 0.2" in stat["sub"] and "max 0.6" in stat["sub"]


_STOCKS = "Stock,Class,ROE,EPS\n" + "\n".join(
    f"S{i:03d},{i % 2},{round(0.1 * i, 2)},{i}" for i in range(1, 41)
)


def test_id_column_yields_top_n_bars() -> None:
    # 40 unique symbols: too many for "category" — classified as an ID column,
    # which yields "Top N <metric> by Stock" bars (the keyed-data charts).
    dash = build_data_dashboard("stocks.csv", _STOCKS)
    bars = [c for c in dash["charts"] if c["kind"] == "bar"]
    assert any("by Stock" in b["title"] for b in bars)
    top = next(b for b in bars if "by Stock" in b["title"])
    assert top["data"][0]["name"] == "S040"  # sorted desc by the metric


def test_custom_chart_specs_compose() -> None:
    dash = build_data_dashboard(
        "stocks.csv",
        _STOCKS,
        extra_charts=[
            {"kind": "bar", "metric": "eps", "by": "stock", "title": "EPS by symbol"},
            {"kind": "line", "metric": "ROE", "by": "Stock"},
            {"kind": "stat", "metric": "roe", "agg": "max"},
            {"kind": "bar", "metric": "nonexistent"},  # unmatched → skipped
        ],
    )
    titles = [c["title"] for c in dash["charts"]]
    assert "EPS by symbol" in titles
    assert "ROE trend" in titles
    line = next(c for c in dash["charts"] if c["title"] == "ROE trend")
    assert line["data"][0]["x"] == "S001"  # labelled by the `by` column
    stat = next(c for c in dash["charts"] if c["title"] == "ROE (max)")
    assert stat["value"] == 4.0


def test_model_sees_a_sample_but_stash_keeps_all_rows() -> None:
    from sila.services.sila.orchestrator.runner import (
        _shrink_tabular_attachments,
        stash_attachments,
    )

    body = "a,b\n" + "\n".join(f"{i},{i}" for i in range(500))
    msg = f"chart this\n\n[Attached file: big.csv]\n{body}\n[End of big.csv]"
    state: dict = {}
    stash_attachments(msg, state)
    assert state["uploaded_files"][0]["content"].count("\n") >= 500  # full file stashed
    small = _shrink_tabular_attachments(msg)
    assert small.count("\n") < 40 and "more rows not shown" in small
    # Non-tabular attachments are untouched.
    prose = "read this\n\n[Attached file: doc.md]\n" + "line\n" * 100 + "[End of doc.md]"
    assert _shrink_tabular_attachments(prose) == prose


def test_pie_share_and_table_specs() -> None:
    dash = build_data_dashboard(
        "stocks.csv",
        _STOCKS,
        extra_charts=[
            {"kind": "pie", "metric": "EPS", "by": "Stock", "title": "EPS share"},
            {"kind": "table", "metric": "ROE", "columns": ["Stock", "ROE", "EPS"], "limit": 5},
        ],
    )
    pie = next(c for c in dash["charts"] if c["title"] == "EPS share")
    assert pie["kind"] == "pie" and pie["data"][0]["name"] == "S040"
    assert any(d["name"] == "Other" for d in pie["data"])  # top 10 + Other
    table = next(c for c in dash["charts"] if c["kind"] == "table")
    assert table["columns"] == ["Stock", "ROE", "EPS"]
    assert len(table["data"]) == 5
    assert table["data"][0]["Stock"] == "S040"  # sorted desc by ROE


def test_grafana_dateless_line_uses_trend_panel() -> None:
    from sila.services.connectors.grafana_dashboard import build_dashboard_with_data

    charts = [
        {"kind": "line", "title": "t", "series": ["v"], "data": [{"x": "S001", "v": 1.0}]},
        {
            "kind": "line",
            "title": "dated",
            "series": ["v"],
            "data": [{"x": "2026-01-01", "v": 1.0}],
        },
        {"kind": "table", "title": "tbl", "columns": ["a"], "data": [{"a": "x"}]},
    ]
    dash = build_dashboard_with_data("X", charts, "ds-1")
    types = [p["type"] for p in dash["panels"]]
    assert types == ["trend", "timeseries", "table"]
    assert "x,v" in dash["panels"][0]["targets"][0]["csvContent"]  # numeric x
    assert "time,v" in dash["panels"][1]["targets"][0]["csvContent"]


async def test_chart_edits_accumulate_across_turns() -> None:
    """ "add a pie" then "add a table": the second turn keeps the first chart —
    the model never re-sends earlier edits (live bug: each edit rebuilt from
    scratch, dropping the pie when the table arrived)."""
    import uuid as _uuid

    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext, execute_tool

    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=_uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=_uuid.uuid4(),
        role=Role.BUILDER,
    )
    ctx.build_state["uploaded_files"] = [{"name": "stocks.csv", "content": _STOCKS}]
    r1 = await execute_tool(
        "create_data_dashboard",
        ctx,
        {"charts": [{"kind": "pie", "metric": "EPS", "by": "Stock", "title": "EPS share"}]},
    )
    titles1 = [c["title"] for c in r1.ui["dashboard"]["charts"]]
    assert "EPS share" in titles1
    r2 = await execute_tool(
        "create_data_dashboard",
        ctx,
        {"charts": [{"kind": "table", "metric": "ROE", "columns": ["Stock", "ROE"]}]},
    )
    charts2 = r2.ui["dashboard"]["charts"]
    titles2 = [c["title"] for c in charts2]
    assert "EPS share" in titles2  # the pie SURVIVED the table edit
    assert any(c["kind"] == "table" for c in charts2)


def test_grafana_pie_panel_slices_per_row() -> None:
    from sila.services.connectors.grafana_dashboard import build_dashboard_with_data

    dash = build_dashboard_with_data(
        "X",
        [{"kind": "pie", "title": "share", "data": [{"name": "A", "value": 1}]}],
        "ds-1",
    )
    opts = dash["panels"][0]["options"]
    # values:true = one slice per ROW (without it Grafana reduces everything
    # into a single solid circle).
    assert opts["reduceOptions"]["values"] is True
    assert dash["panels"][0]["type"] == "piechart"


def test_custom_line_over_category_aggregates_per_label() -> None:
    # Live bug: line with a category axis drew one point per RAW row
    # ("West West West…"). One aggregated point per label instead.
    text = "region,profit\nWest,10\nWest,20\nEast,30\nEast,50\n"
    header, rows = parse_csv(text)
    cols = profile_columns(header, rows)
    from sila.services.sila.data_dashboard import _custom_chart

    chart = _custom_chart(cols, {"kind": "line", "metric": "profit", "by": "region"})
    assert chart is not None
    assert [d["x"] for d in chart["data"]] == ["West", "East"]
    assert [d["profit"] for d in chart["data"]] == [15.0, 40.0]
