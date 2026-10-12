"""Power BI push-dataset connector — schema inference, chunked push, tool paths.

No live Power BI needed: the service takes an injectable ConnectorService, so a
fake records every proxied call; the tool tests exercise the REAL import path
(the class of bug that once wedged a turn on a bad Grafana import).
"""

from __future__ import annotations

import uuid

import pytest

from sila.services.connectors import powerbi_service as pbi_module
from sila.services.connectors.manifest import PAYLOAD_BUILT, spec_for
from sila.services.connectors.powerbi_service import (
    PowerBIError,
    PowerBIService,
    _dedupe_names,
    _infer_schema,
    _serialize_rows,
)
from sila.services.sila.data_dashboard import parse_csv, profile_columns


@pytest.fixture(autouse=True)
def _fast_import_polls(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(pbi_module, "_IMPORT_POLL_S", 0)


@pytest.fixture(autouse=True)
def _stub_pbix_upload(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The pbix upload goes DIRECT to api.powerbi.com (Nango can't multipart) —
    stub it by default so no test ever leaves the box."""

    async def _ok(self, token, title, body, content_type):  # type: ignore[no-untyped-def]
        return {"id": "imp-1"}

    monkeypatch.setattr(PowerBIService, "_upload_pbix", _ok)


_CSV = "region,amount,count,when\nNorth,10.5,3,2024-01-02\nSouth,20,4,2024-01-03\n,30.25,5,\n"


# --- manifest -----------------------------------------------------------------


def test_manifest_has_powerbi_connect_only() -> None:
    spec = spec_for("powerbi")
    assert spec is not None
    assert spec.label == "Power BI"
    assert spec.auth_type == "oauth"
    assert spec.api_base_url == "https://api.powerbi.com"  # own host, not Graph
    assert spec.action is None  # data tool, not a publisher action
    assert "powerbi" not in PAYLOAD_BUILT  # real endpoints pass through identity


# --- pure helpers ---------------------------------------------------------------


def _profiled(csv_text: str):  # type: ignore[no-untyped-def]
    header, rows = parse_csv(csv_text)
    return profile_columns(header, rows)


def test_schema_maps_kinds_to_powerbi_datatypes() -> None:
    schema = _infer_schema(_profiled(_CSV))
    by_name = {s["name"]: s["dataType"] for s in schema}
    assert by_name == {
        "region": "String",
        "amount": "Double",  # 10.5 is fractional -> Double
        "count": "Int64",  # every value integral -> Int64
        "when": "DateTime",
    }


def test_rows_serialize_nulls_ints_and_iso_dates() -> None:
    cols = _profiled(_CSV)
    rows = _serialize_rows(cols, _infer_schema(cols))
    assert rows[0] == {
        "region": "North",
        "amount": 10.5,
        "count": 3,  # Int64 -> real int, not 3.0
        "when": "2024-01-02T00:00:00",
    }
    # Blank region / blank date -> JSON null, never "" or "NaN".
    assert rows[2]["region"] is None and rows[2]["when"] is None
    assert rows[2]["amount"] == 30.25


def test_duplicate_headers_are_suffixed() -> None:
    assert _dedupe_names(["Name", "Name", "name"]) == ["Name", "Name (2)", "name (3)"]


def test_all_text_csv_is_a_valid_all_string_schema() -> None:
    schema = _infer_schema(_profiled("a,b\nx,y\nz,w\n"))
    assert {s["dataType"] for s in schema} == {"String"}


async def test_more_than_75_columns_rejected_before_any_api_call() -> None:
    header = ",".join(f"c{i}" for i in range(76))
    row = ",".join("1" for _ in range(76))
    fake = _FakeConnectors()
    svc = PowerBIService(db=None, connectors=fake)  # type: ignore[arg-type]
    with pytest.raises(PowerBIError, match="75 columns"):
        await svc.push_csv_dataset(
            user_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            filename="wide.csv",
            csv_text=f"{header}\n{row}\n",
            title="Wide",
        )
    assert fake.calls == []  # rejected before touching the proxy


# --- service ------------------------------------------------------------------


class _FakeConnectors:
    """Records execute_proxy calls; answers the dataset create + import poll."""

    def __init__(self, fail_put: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail_put = fail_put

    async def fetch_access_token(self, **kw: object) -> str:
        self.calls.append({"method": "TOKEN", "endpoint": "credentials", **kw})
        return "tok"

    async def execute_proxy(self, **kw: object) -> dict:
        self.calls.append(kw)
        endpoint, method = str(kw["endpoint"]), kw["method"]
        if self._fail_put and method == "PUT":
            from sila.adapters.connectors.nango_client import NangoError

            raise NangoError("dataset gone")
        if method == "POST" and endpoint == "v1.0/myorg/datasets":
            return {"id": "ds-1"}
        if method == "GET" and endpoint == "v1.0/myorg/imports/imp-1":
            return {
                "importState": "Succeeded",
                "reports": [{"id": "rep-1", "webUrl": "https://app.powerbi.com/x/rep-1"}],
            }
        return {}


async def _push(fake: _FakeConnectors, *, dataset_id: str | None = None, csv: str = _CSV):  # type: ignore[no-untyped-def]
    svc = PowerBIService(db=None, connectors=fake)  # type: ignore[arg-type]
    return await svc.push_csv_dataset(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        filename="sales.csv",
        csv_text=csv,
        title="Sales",
        dataset_id=dataset_id,
    )


async def test_create_path_posts_push_dataset_then_rows() -> None:
    fake = _FakeConnectors()
    result = await _push(fake)
    create, rows_call = fake.calls[0], fake.calls[1]
    assert (create["method"], create["endpoint"]) == ("POST", "v1.0/myorg/datasets")
    body = create["data"]
    assert body["defaultMode"] == "Push" and body["name"] == "Sales"
    assert body["tables"][0]["name"] == "Data" and len(body["tables"][0]["columns"]) == 4
    assert rows_call["endpoint"] == "v1.0/myorg/datasets/ds-1/tables/Data/rows"
    assert len(rows_call["data"]["rows"]) == 3
    assert result.dataset_id == "ds-1" and result.updated is False
    assert result.url == "https://app.powerbi.com/groups/me/datasets/ds-1/details"
    assert (result.rows, result.columns) == (3, 4)


async def test_report_is_uploaded_direct_with_token_and_rebound(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict = {}

    async def _spy(self, token, title, body, content_type):  # type: ignore[no-untyped-def]
        captured.update(token=token, title=title, body=body, content_type=content_type)
        return {"id": "imp-1"}

    monkeypatch.setattr(PowerBIService, "_upload_pbix", _spy)
    fake = _FakeConnectors()
    result = await _push(fake)
    assert captured["token"] == "tok"  # fetched via the audited escape hatch
    assert str(captured["content_type"]).startswith("multipart/form-data; boundary=")
    assert b'filename="Sales.pbix"' in captured["body"]
    assert any(str(c["endpoint"]).endswith("/Rebind") for c in fake.calls)
    assert result.report_url == "https://app.powerbi.com/x/rep-1"
    assert result.report_failed is False


async def test_report_failure_is_graceful_data_still_delivered(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def _boom(self, token, title, body, content_type):  # type: ignore[no-untyped-def]
        raise PowerBIError("import upload failed (400): unsupported pbix")

    monkeypatch.setattr(PowerBIService, "_upload_pbix", _boom)
    fake = _FakeConnectors()
    result = await _push(fake)
    assert result.dataset_id == "ds-1"  # the push itself never depends on the report
    assert result.report_url is None and result.report_failed is True


async def test_report_kill_switch_skips_the_import(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sila.core.config import settings

    monkeypatch.setattr(settings, "powerbi_report_generation", False)
    fake = _FakeConnectors()
    result = await _push(fake)
    assert not any("imports" in str(c["endpoint"]) for c in fake.calls)
    assert result.report_url is None and result.report_failed is False


async def test_rows_are_chunked_under_the_api_cap() -> None:
    lines = ["n"] + ["1"] * 6_000  # NO row cap here — Power BI gets every row
    fake = _FakeConnectors()
    result = await _push(fake, csv="\n".join(lines) + "\n")
    row_calls = [c for c in fake.calls if c["endpoint"].endswith("/rows")]
    # Chat-parity fix: all 6,000 rows push (the old 5k parse cap made Power BI
    # aggregate a different sample than the chat dashboard).
    assert [len(c["data"]["rows"]) for c in row_calls] == [2_500, 2_500, 1_000]
    assert result.rows == 6_000


async def test_update_path_refreshes_in_place() -> None:
    fake = _FakeConnectors()
    result = await _push(fake, dataset_id="old-ds")
    methods = [(c["method"], c["endpoint"]) for c in fake.calls]
    assert methods[0] == ("PUT", "v1.0/myorg/datasets/old-ds/tables/Data")
    assert methods[1] == ("DELETE", "v1.0/myorg/datasets/old-ds/tables/Data/rows")
    assert methods[2][1] == "v1.0/myorg/datasets/old-ds/tables/Data/rows"
    assert all(ep != "v1.0/myorg/datasets" for _, ep in methods)  # no create
    assert result.updated is True and result.dataset_id == "old-ds"


async def test_deleted_dataset_falls_back_to_create() -> None:
    fake = _FakeConnectors(fail_put=True)
    result = await _push(fake, dataset_id="old-ds")
    # PUT failed -> a fresh dataset is created instead of failing the turn.
    assert any(c["endpoint"] == "v1.0/myorg/datasets" for c in fake.calls)
    assert result.updated is False and result.dataset_id == "ds-1"


# --- the thin PBIX builder (reverse-engineered format) --------------------------


def test_thin_pbix_is_a_valid_zip_with_utf16_layout() -> None:
    import io
    import json
    import zipfile

    from sila.services.connectors.powerbi_report import build_thin_pbix

    cols = _profiled(_CSV)
    pbix = build_thin_pbix("ds-42", "Sales", cols)
    with zipfile.ZipFile(io.BytesIO(pbix)) as z:
        names = set(z.namelist())
        assert {"Version", "[Content_Types].xml", "Connections", "Report/Layout"} <= names
        # Connections (UTF-8) carries the dataset id + live-connection type.
        conn = json.loads(z.read("Connections"))
        assert conn["Connections"][0]["PbiModelDatabaseName"] == "ds-42"
        assert conn["Connections"][0]["ConnectionType"] == "pbiServiceLive"
        # Layout is UTF-16LE JSON; every visual config parses and references
        # real profiled columns on the push dataset's "Data" table.
        layout = json.loads(z.read("Report/Layout").decode("utf-16-le"))
        containers = layout["sections"][0]["visualContainers"]
        assert containers, "expected at least one visual"
        for vc in containers:
            cfg = json.loads(vc["config"])
            query = cfg["singleVisual"]["prototypeQuery"]
            assert query["From"][0]["Entity"] == "Data"
        all_cfg = " ".join(vc["config"] for vc in containers)
        assert "amount" in all_cfg and "region" in all_cfg


def test_pick_visuals_covers_cards_bars_line_and_table() -> None:
    import json

    from sila.services.connectors.powerbi_report import pick_visuals

    # region=category, amount/count=number, when=date → cards + bar + line/pie.
    visuals = pick_visuals(_profiled(_CSV))
    kinds = [json.loads(vc["config"])["singleVisual"]["visualType"] for vc in visuals]
    assert "card" in kinds and "clusteredBarChart" in kinds
    assert "lineChart" in kinds or "pieChart" in kinds
    assert "tableEx" in kinds  # the raw data rides along at the bottom


def test_bars_sort_descending_by_the_metric() -> None:
    import json

    from sila.services.connectors.powerbi_report import pick_visuals

    visuals = pick_visuals(_profiled(_CSV))
    bars = [
        json.loads(vc["config"])["singleVisual"]
        for vc in visuals
        if json.loads(vc["config"])["singleVisual"]["visualType"] == "clusteredBarChart"
    ]
    assert bars, "expected a bar visual"
    order = bars[0]["prototypeQuery"]["OrderBy"]
    assert order[0]["Direction"] == 2  # descending — top performers first


def test_extra_charts_mirror_the_chat_dashboard() -> None:
    import json

    from sila.services.connectors.powerbi_report import pick_visuals

    # The user asked for a pie + a custom table in chat — Power BI mirrors both.
    visuals = pick_visuals(
        _profiled(_CSV),
        extra_charts=[
            {"kind": "pie", "metric": "amount", "by": "region"},
            {"kind": "table", "columns": ["region", "amount"]},
        ],
    )
    kinds = [json.loads(vc["config"])["singleVisual"]["visualType"] for vc in visuals]
    assert kinds.count("pieChart") >= 1
    tables = [
        json.loads(vc["config"])["singleVisual"]
        for vc in visuals
        if json.loads(vc["config"])["singleVisual"]["visualType"] == "tableEx"
    ]
    assert tables, "expected the requested table"
    refs = json.dumps(tables[0]["projections"])
    assert "region" in refs and "amount" in refs
    # The page grows to fit — nothing may overlap the section bottom.
    assert all(int(v["y"]) >= 0 for v in visuals)


def test_junk_and_binary_columns_never_become_visuals() -> None:
    import json

    from sila.services.connectors.powerbi_report import pick_visuals

    # col1 = auto-named blank header; Class = 0/1 flag; roe = the real metric.
    csv = "sym,col1,Class,roe\nNVO,5,1,4.2\nMU,7,0,2.1\nAAPL,9,1,3.3\n"
    visuals = pick_visuals(_profiled(csv))
    configs = " ".join(vc["config"] for vc in visuals)
    cards_and_bars = [
        json.loads(vc["config"])["singleVisual"]
        for vc in visuals
        if json.loads(vc["config"])["singleVisual"]["visualType"] in ("card", "clusteredBarChart")
    ]
    assert cards_and_bars, "expected visuals from the real metric"
    for sv in cards_and_bars:
        blob = json.dumps(sv)
        assert "col1" not in blob and '"Class"' not in blob
    assert "roe" in configs


# --- the SILA tool (real import path) -------------------------------------------


class _NoRowResult:
    def scalar_one_or_none(self) -> None:
        return None


class _FakeDB:
    async def execute(self, _stmt: object) -> _NoRowResult:
        return _NoRowResult()


def _ctx(db: object = None):  # type: ignore[no-untyped-def]
    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext

    return ToolContext(
        db=db,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.BUILDER,
    )


async def test_tool_asks_for_a_file_when_none_attached() -> None:
    from sila.services.sila.tool_registry import execute_tool

    res = await execute_tool("create_powerbi_report", _ctx(), {})
    assert "No data file is attached" in res.content


async def test_tool_reports_not_connected_cleanly() -> None:
    """No Power BI binding -> the Connectors-page message, never a stack trace."""
    from sila.services.sila.tool_registry import execute_tool

    ctx = _ctx(db=_FakeDB())
    ctx.build_state["uploaded_files"] = [{"name": "sales.csv", "content": _CSV}]
    res = await execute_tool("create_powerbi_report", ctx, {})
    assert "Connectors" in res.content and res.summary == "Power BI not connected"


async def test_tool_persists_dataset_identity_for_continuity(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sila.services.connectors.powerbi_service import PushResult
    from sila.services.sila.tool_registry import execute_tool

    async def _fake_push(self, **kw):  # type: ignore[no-untyped-def]
        return PushResult(dataset_id="ds-9", url="https://x/ds-9", rows=3, columns=4, updated=False)

    monkeypatch.setattr(PowerBIService, "push_csv_dataset", _fake_push)
    ctx = _ctx(db=_FakeDB())
    ctx.build_state["uploaded_files"] = [{"name": "sales.csv", "content": _CSV}]
    res = await execute_tool("create_powerbi_report", ctx, {"title": "Sales"})
    assert ctx.build_state["powerbi_dataset_id"] == "ds-9"
    assert ctx.build_state["powerbi_dataset_title"] == "Sales"
    assert "https://x/ds-9" in res.content and "Auto-create" in res.content
