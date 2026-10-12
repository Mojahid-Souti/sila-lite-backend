"""Analysis Engine P0 — DuckDB full-file path vs the proven stdlib v1 path.

Contract under test (the spec's Phase-0 done-when): chat ATTACHMENTS keep the
byte-identical v1 stdlib profiler; uploaded DATASETS run on DuckDB over the
FULL file with the SAME output shape; the runner stashes dataset handles; the
upload endpoint is auth-gated.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sila.main import app
from sila.services.analysis import engine
from sila.services.sila.data_dashboard import build_data_dashboard, parse_csv, profile_columns

GOLDEN = Path(__file__).parent / "fixtures" / "analysis" / "golden_sales.csv"

client = TestClient(app)


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


class _Row:
    def __init__(self, path: str) -> None:
        self.path = path


# ---------------------------------------------------------------------------
# Engine parity — same profile kinds and same chart plan as the stdlib v1
# ---------------------------------------------------------------------------


def test_profile_kinds_match_stdlib() -> None:
    text = GOLDEN.read_text()
    header, rows = parse_csv(text)
    stdlib_kinds = {c.name: c.kind for c in profile_columns(header, rows)}
    duck = engine.profile_dataset(str(GOLDEN))
    duck_kinds = {c["name"]: c["kind"] for c in duck["columns"]}
    assert duck_kinds == stdlib_kinds
    assert duck["rows"] == len(rows)


def test_dashboard_parity_with_stdlib() -> None:
    v1 = build_data_dashboard("golden_sales.csv", GOLDEN.read_text())
    duck = engine.dashboard_from_dataset(str(GOLDEN), "golden_sales.csv")
    # Same visual plan: identical (kind, title) sequence…
    assert [(c["kind"], c["title"]) for c in duck["charts"]] == [
        (c["kind"], c["title"]) for c in v1["charts"]
    ]
    # …and identical ground-truth stat cards (the numbers the model quotes).
    v1_stats = [c for c in v1["charts"] if c["kind"] == "stat"]
    duck_stats = [c for c in duck["charts"] if c["kind"] == "stat"]
    assert duck_stats == v1_stats


def test_provenance_recorded() -> None:
    from sila.services.analysis.state import ProvenanceLedger

    ledger = ProvenanceLedger()
    engine.dashboard_from_dataset(str(GOLDEN), "golden_sales.csv", provenance=ledger)
    assert ledger.entries, "every computed artifact must leave a provenance trail"
    assert all(any("duckdb:" in op for op in e.operations) for e in ledger.entries)


def test_export_csv_sample_is_honest() -> None:
    full, sampled = engine.export_csv_sample(str(GOLDEN), limit=50_000)
    assert not sampled and full.count("\n") >= 8
    head, capped = engine.export_csv_sample(str(GOLDEN), limit=3)
    assert capped and len(head.strip().splitlines()) == 4  # header + 3 rows


def test_unusable_dataset_raises() -> None:
    with pytest.raises(ValueError):
        engine.dashboard_from_dataset("/nonexistent/nope.csv", "nope.csv")


# ---------------------------------------------------------------------------
# Runner — dataset markers stash a handle (no bytes) next to file bodies
# ---------------------------------------------------------------------------


def test_stash_parses_dataset_markers() -> None:
    from sila.services.sila.orchestrator.runner import stash_attachments

    ds_id = str(uuid.uuid4())
    state: dict = {}
    msg = (
        "chart this\n"
        "[Attached file: notes.txt]\nhello\n[End of notes.txt]\n"
        f"[Attached dataset: big_sales.csv dataset_id={ds_id} rows=200000]"
    )
    assert stash_attachments(msg, state)
    files = state["uploaded_files"]
    assert files[0] == {"name": "notes.txt", "content": "hello"}
    assert files[1] == {
        "name": "big_sales.csv",
        "content": "",
        "dataset_id": ds_id,
        "rows": 200000,
    }


# ---------------------------------------------------------------------------
# Resolver — attachments keep v1; dataset handles route to the engine
# ---------------------------------------------------------------------------


async def test_spec_for_chosen_attachment_is_v1_byte_identical() -> None:
    from sila.services.sila.tool_registry import _spec_for_chosen

    text = GOLDEN.read_text()
    spec = await _spec_for_chosen(_ctx(), {"name": "golden_sales.csv", "content": text})
    assert spec == build_data_dashboard("golden_sales.csv", text)


async def test_spec_for_chosen_dataset_routes_to_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    from sila.db.repositories.sila import SilaRepository
    from sila.services.sila.tool_registry import _spec_for_chosen

    async def _get(self: Any, dataset_id: uuid.UUID, user_id: uuid.UUID) -> _Row:
        return _Row(str(GOLDEN))

    monkeypatch.setattr(SilaRepository, "get_dataset", _get)
    chosen = {"name": "golden_sales.csv", "content": "", "dataset_id": str(uuid.uuid4())}
    spec = await _spec_for_chosen(_ctx(), chosen)
    assert spec["source"] == "golden_sales.csv"
    kinds = {c["kind"] for c in spec["charts"]}
    assert "stat" in kinds and len(spec["charts"]) >= 3


async def test_spec_for_chosen_missing_dataset_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    from sila.db.repositories.sila import SilaRepository
    from sila.services.sila.tool_registry import _spec_for_chosen

    async def _gone(self: Any, dataset_id: uuid.UUID, user_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(SilaRepository, "get_dataset", _gone)
    with pytest.raises(ValueError, match="re-upload"):
        await _spec_for_chosen(
            _ctx(), {"name": "x.csv", "content": "", "dataset_id": str(uuid.uuid4())}
        )


# ---------------------------------------------------------------------------
# Endpoint — auth-gated, extension-gated
# ---------------------------------------------------------------------------


def test_dataset_upload_unauthenticated() -> None:
    r = client.post(
        f"/v1/sila/datasets?workspace_id={uuid.uuid4()}",
        files={"file": ("sales.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert r.status_code in (401, 403)


def test_an_uploaded_workbook_gets_a_csv_beside_it(tmp_path) -> None:
    from sila.api.v1.sila import _workbook_to_dataset_csv
    from tests.test_xlsx import _num, _row, _workbook

    header = '<c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
    values = '<c r="A2" t="s"><v>2</v></c>' + _num("B2", "120")
    workbook = tmp_path / "d.xlsx"
    workbook.write_bytes(
        _workbook(
            [("Sales", _row(1, header) + _row(2, values))], shared=["region", "sales", "Muscat"]
        )
    )
    _workbook_to_dataset_csv(str(workbook), str(tmp_path / "d.csv"))
    assert workbook.is_file(), "the original is kept (the OKR pack reads it)"
    lines = (tmp_path / "d.csv").read_text().splitlines()
    assert lines[0] == "region,sales"
    assert lines[1] == "Muscat,120"


def test_a_file_that_is_not_a_workbook_is_refused_and_removed(tmp_path) -> None:
    from fastapi import HTTPException

    from sila.api.v1.sila import _workbook_to_dataset_csv

    fake = tmp_path / "d.xlsx"
    fake.write_bytes(b"this is not a zip")
    with pytest.raises(HTTPException) as refused:
        _workbook_to_dataset_csv(str(fake), str(tmp_path / "d.csv"))
    assert refused.value.status_code == 422
    assert not fake.exists() and not (tmp_path / "d.csv").exists()
