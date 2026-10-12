"""Grafana dashboard JSON generation + URL validation."""

from __future__ import annotations

import pytest

from sila.services.connectors.grafana_dashboard import build_dashboard
from sila.services.connectors.grafana_service import GrafanaConfigError, _validate_url


def test_builds_grid_and_panel_types() -> None:
    dash = build_dashboard(
        "Ops",
        [
            {"title": "CPU", "type": "timeseries", "query": "rate(cpu[5m])", "unit": "percent"},
            {"title": "Errors", "type": "stat", "query": "sum(errors)"},
            {"title": "Notes", "type": "text", "text": "# heading"},
        ],
        datasource_uid="prom-1",
    )
    assert dash["title"] == "Ops"
    panels = dash["panels"]
    assert [p["type"] for p in panels] == ["timeseries", "stat", "text"]
    # Two per row: (0,0), (12,0), (0,8).
    assert (panels[0]["gridPos"]["x"], panels[0]["gridPos"]["y"]) == (0, 0)
    assert (panels[1]["gridPos"]["x"], panels[1]["gridPos"]["y"]) == (12, 0)
    assert (panels[2]["gridPos"]["x"], panels[2]["gridPos"]["y"]) == (0, 8)
    # Data panel carries the query + datasource; text panel carries content.
    assert panels[0]["targets"][0]["expr"] == "rate(cpu[5m])"
    assert panels[0]["datasource"]["uid"] == "prom-1"
    assert panels[0]["fieldConfig"]["defaults"]["unit"] == "percent"
    assert panels[2]["options"]["content"] == "# heading"


def test_unknown_type_falls_back_to_timeseries() -> None:
    dash = build_dashboard("X", [{"title": "P", "type": "wat", "query": "q"}])
    assert dash["panels"][0]["type"] == "timeseries"


def test_empty_title_and_no_panels_raise() -> None:
    with pytest.raises(ValueError, match="title"):
        build_dashboard("  ", [{"title": "P", "type": "stat"}])
    with pytest.raises(ValueError, match="panel"):
        build_dashboard("X", [])


def test_non_dict_panels_are_skipped() -> None:
    dash = build_dashboard("X", ["bad", {"title": "ok", "type": "stat"}])  # type: ignore[list-item]
    assert len(dash["panels"]) == 1


def test_panel_cap() -> None:
    many = [{"title": f"p{i}", "type": "stat"} for i in range(40)]
    dash = build_dashboard("X", many)
    assert len(dash["panels"]) == 24


# --- URL validation (SSRF-appropriate for on-prem Grafana) ---


def test_url_allows_private_lan_and_localhost() -> None:
    for ok in (
        "http://localhost:3000",
        "http://grafana:3000/",
        "https://grafana.company.internal",
        "http://192.168.1.50:3000",
    ):
        assert _validate_url(ok) == ok.rstrip("/")


def test_url_blocks_metadata_and_bad_schemes() -> None:
    for bad in (
        "http://169.254.169.254",  # cloud metadata (link-local)
        "http://metadata.google.internal",
        "ftp://grafana:3000",
        "grafana:3000",  # no scheme
        "https://",  # no host
    ):
        with pytest.raises(GrafanaConfigError):
            _validate_url(bad)


# --- the SILA tool end-to-end (would have caught the bad import that wedged a turn) ---


class _NoRowResult:
    def scalar_one_or_none(self) -> None:
        return None


class _FakeDB:
    async def execute(self, _stmt: object) -> _NoRowResult:
        return _NoRowResult()


def _ctx(db: object = None):  # type: ignore[no-untyped-def]
    import uuid as _uuid

    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext

    return ToolContext(
        db=db,  # type: ignore[arg-type]
        user=UserResult(id=_uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=_uuid.uuid4(),
        role=Role.BUILDER,
    )


async def test_tool_reports_not_connected_cleanly() -> None:
    """Exercises the real import + service path: no Grafana connected -> a clear
    message, not an exception (the live bug was a ModuleNotFoundError here)."""
    from sila.services.sila.tool_registry import execute_tool

    res = await execute_tool(
        "create_grafana_dashboard",
        _ctx(db=_FakeDB()),
        {"title": "Ops", "panels": [{"title": "p", "type": "stat"}]},
    )
    assert "No Grafana is connected" in res.content


async def test_tool_validates_args_before_touching_grafana() -> None:
    from sila.services.sila.tool_registry import execute_tool

    no_title = await execute_tool("create_grafana_dashboard", _ctx(), {"panels": [{}]})
    assert "needs a title" in no_title.content
    no_panels = await execute_tool("create_grafana_dashboard", _ctx(), {"title": "X"})
    assert "at least one panel" in no_panels.content


async def test_execute_tool_safety_net(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A tool that crashes or hangs becomes a clean error result — never a wedged
    turn (the browser timer counted 800s+ on the live bug)."""
    import asyncio

    from sila.services.sila import tool_registry as tr

    async def _boom(_ctx, _args):  # type: ignore[no-untyped-def]
        raise RuntimeError("kaboom")

    async def _hang(_ctx, _args):  # type: ignore[no-untyped-def]
        await asyncio.sleep(60)

    crash_tool = next(iter(tr.TOOLS.values()))
    monkeypatch.setitem(
        tr.TOOLS, "boom", type(crash_tool)(**{**crash_tool.__dict__, "name": "boom", "run": _boom})
    )
    monkeypatch.setitem(
        tr.TOOLS, "hang", type(crash_tool)(**{**crash_tool.__dict__, "name": "hang", "run": _hang})
    )
    monkeypatch.setattr(tr, "_TOOL_TIMEOUT_S", 0.2)

    crashed = await tr.execute_tool("boom", _ctx(), {})
    assert "failed" in crashed.content.lower()
    assert crashed.summary == "Failed"
    hung = await tr.execute_tool("hang", _ctx(), {})
    assert "timed out" in hung.content
    # The turn must not wedge — but the model must also not be handed internals
    # to relay. This used to assert "RuntimeError" was PRESENT, which is how
    # "an unexpected error with the search_bazaar function" reached a user.
    for res, tool in ((crashed, "boom"), (hung, "hang")):
        assert "RuntimeError" not in res.content
        assert tool not in res.content


async def test_progress_notes_reach_the_channel() -> None:
    """ctx.report streams REAL stage notes (service stages included) to the trace."""
    from sila.services.sila.tool_registry import execute_tool

    notes: list[str] = []
    ctx = _ctx(db=_FakeDB())
    ctx.progress = notes.append
    await execute_tool(
        "create_grafana_dashboard",
        ctx,
        {"title": "Ops", "panels": [{"title": "p", "type": "stat"}]},
    )
    assert notes and "Grafana connection" in notes[0]


def test_panels_carry_datasource_type_and_uid() -> None:
    dash = build_dashboard(
        "X",
        [{"title": "p", "type": "stat", "query": "q"}],
        datasource_uid="prom-1",
        datasource_type="prometheus",
    )
    ds = dash["panels"][0]["datasource"]
    assert ds == {"type": "prometheus", "uid": "prom-1"}


# --- public share links (Stage A: the guaranteed-viewable, no-sign-in URL) ---


def _client_with(handler):  # type: ignore[no-untyped-def]
    import httpx

    from sila.adapters.connectors.grafana_client import GrafanaClient

    client = GrafanaClient("http://g", "tok")

    real = httpx.AsyncClient

    def factory(*_a, **_kw):  # type: ignore[no-untyped-def]
        return real(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(5.0))

    return client, factory


async def test_public_dashboard_created(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/public-dashboards")
        if req.method == "GET":
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json={"accessToken": "abc123"})

    client, factory = _client_with(handler)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    url = await client.ensure_public_dashboard("dash-1")
    assert url == "http://g/public-dashboards/abc123"


async def test_public_dashboard_conflict_reuses_existing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        # An existing share is found by the GET and kept — POST never fires.
        assert req.method == "GET"
        return httpx.Response(200, json={"accessToken": "kept"})

    client, factory = _client_with(handler)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    url = await client.ensure_public_dashboard("dash-1")
    assert url == "http://g/public-dashboards/kept"


async def test_public_dashboard_failure_returns_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    client, factory = _client_with(handler)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert await client.ensure_public_dashboard("dash-1") is None


# --- layout: every row fills the grid ---------------------------------------


def _boxes(kinds: list[str]) -> list[dict[str, int]]:
    from sila.services.connectors.grafana_dashboard import _layout

    return _layout([{"kind": k, "data": []} for k in kinds])


def _rows(boxes: list[dict[str, int]]) -> dict[int, int]:
    """y → total width on that row."""
    widths: dict[int, int] = {}
    for box in boxes:
        widths[box["y"]] = widths.get(box["y"], 0) + box["w"]
    return widths


def test_every_row_fills_the_full_grid() -> None:
    """A short row is not just ugly: Grafana compacts panels upward into free
    space, so the next chart floats up beside a stray stat and the dashboard
    reads as though the panels landed at random."""
    for kinds in (
        ["stat"] * 5 + ["bar", "pie", "line"],
        ["stat"] * 4 + ["bar", "pie"],
        ["stat"] * 3 + ["bar"],
        ["stat"] * 7 + ["bar"],
        ["stat", "bar"],
    ):
        for y, total in _rows(_boxes(kinds)).items():
            assert total == 24, f"{kinds} leaves {24 - total} columns free at y={y}"


def test_a_lone_fifth_stat_does_not_strand_a_row() -> None:
    """Five KPIs used to place four across and one alone at width 6, leaving 18
    columns empty — the exact gap the reported layout came from."""
    boxes = _boxes(["stat"] * 5)
    assert len({b["y"] for b in boxes}) == 2
    assert all(len([b for b in boxes if b["y"] == y]) >= 2 for y in {b["y"] for b in boxes})


def test_stats_sit_above_every_chart() -> None:
    boxes = _boxes(["bar", "stat", "pie", "stat"])
    highest_chart = min(
        b["y"] for b, k in zip(boxes, ["bar", "stat", "pie", "stat"], strict=True) if k != "stat"
    )
    lowest_stat = max(
        b["y"] for b, k in zip(boxes, ["bar", "stat", "pie", "stat"], strict=True) if k == "stat"
    )
    assert lowest_stat < highest_chart


def test_kpi_row_is_short_so_charts_stay_above_the_fold() -> None:
    boxes = _boxes(["stat"] * 4 + ["bar"])
    assert all(b["h"] == 4 for b in boxes[:4])


# --- KPI direction ----------------------------------------------------------


def _dashboard(csv: str) -> dict:
    from sila.services.connectors.grafana_dashboard import build_dashboard_with_data
    from sila.services.sila.data_dashboard import build_data_dashboard

    return build_dashboard_with_data("D", build_data_dashboard("d.csv", csv)["charts"], "uid")


def _stat_colours(dash: dict) -> dict[str, str]:
    return {
        p["title"]: p["fieldConfig"]["defaults"]["color"]["fixedColor"]
        for p in dash["panels"]
        if p["type"] == "stat"
    }


_RISING = "date,units\n" + "\n".join(f"2025-0{i + 1}-01,{100 + i * 20}" for i in range(8))
_FALLING = "date,units\n" + "\n".join(f"2025-0{i + 1}-01,{300 - i * 20}" for i in range(8))


def test_a_rising_metric_is_green_and_a_falling_one_red() -> None:
    assert set(_stat_colours(_dashboard(_RISING)).values()) == {"green"}
    assert set(_stat_colours(_dashboard(_FALLING)).values()) == {"red"}


def test_the_change_is_shown_on_the_panel() -> None:
    titles = list(_stat_colours(_dashboard(_RISING)))
    assert any("▲" in t and "%" in t for t in titles)


def test_undated_data_never_borrows_the_direction_colours() -> None:
    """No date column means no comparison. The KPI still takes an accent so the
    strip does not read as unfinished — but never green or red, which mean
    "rising" and "falling" on this dashboard. A decorative colour must not be
    mistaken for a verdict."""
    colours = set(_stat_colours(_dashboard("brand,units\nA,10\nB,20\nC,30\nD,40")).values())
    assert colours
    assert not (colours & {"green", "red"})


def test_a_series_too_short_to_compare_never_borrows_them_either() -> None:
    colours = set(_stat_colours(_dashboard("date,units\n2025-01-01,10\n2025-02-01,20")).values())
    assert not (colours & {"green", "red"})


def test_a_radar_fallback_is_grouped_never_stacked() -> None:
    """Stacking only ever fired on the radar fallback, and that is the one bar
    it must not fire on: a radar's series are each metric scaled to 0-100 on its
    own terms, so they share no unit. Stacked they summed into "100 / 91.7 /
    400" rows that meant nothing. Grouped, the comparison survives."""
    # Enough rows and metrics for the profiler to emit a multi-metric chart
    # alongside the plain breakdowns — with only a handful it emits neither.
    csv = (
        "country,region,population_m,gdp_usd_b,life_expectancy\n"
        "China,Asia-Pacific,1411,17963,78\nUnited States,Americas,335,27360,79\n"
        "India,Asia-Pacific,1428,3550,70\nGermany,Europe,84,4460,81\n"
        "Japan,Asia-Pacific,124,4230,84\nBrazil,Americas,216,2170,75\n"
        "Nigeria,Africa,223,363,53\n"
    )
    panels = [p for p in _dashboard(csv)["panels"] if p["type"] == "barchart"]
    assert panels, "the fixture should produce bar panels"
    assert {p["options"]["stacking"] for p in panels} == {"none"}
    # A radar-derived bar still shows every series, it just sits them side by
    # side — so the legend has to stay on.
    multi = [p for p in panels if p["options"]["legend"]["showLegend"]]
    assert multi, "a multi-series bar keeps its legend"
