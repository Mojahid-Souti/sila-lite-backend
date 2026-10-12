"""Power BI connector service — push a CSV into the user's own Power BI.

The SILA data tool's backend: parse + profile the attached file (the same
deterministic pipeline the in-Sila dashboards use), infer a push-dataset schema,
create the dataset in the user's **My workspace**, and push the rows in chunks.
One click on the returned dataset page ("Create a report" → Auto-create) builds
the visuals — v1 pushes data only, no reports/visuals API.

Transport: every HTTP hop rides `ConnectorService.execute_proxy` (the per-user
Nango OAuth rails, like the OneDrive/PowerPoint saves) — binding-checked,
audited, and host-pinned to api.powerbi.com via the manifest's Base-Url-Override.

Update semantics: a dataset id from an earlier turn is refreshed IN PLACE
(schema PUT → rows DELETE → re-push) so same-session rebuilds never stack
duplicates; if that dataset was deleted in the Power BI UI, we fall back to a
fresh create.

Complexity: O(rows × columns) to serialize; ceil(rows/2500) + 1 HTTP calls.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from sila.core.config import settings
from sila.services.sila.data_dashboard import Column, parse_csv, profile_columns

log = structlog.get_logger(__name__)

_TABLE = "Data"  # single fixed table: a predictable target for update/delete
_CHUNK_ROWS = 2_500  # well under the API's 10,000-rows-per-request cap
_MAX_COLUMNS = 75  # Power BI push-dataset hard limit
_DATASET_URL = "https://app.powerbi.com/groups/me/datasets/{id}/details"
_MAX_INT64 = 2**53  # beyond this a float can't represent the integer exactly
_IMPORT_POLLS = 25  # × _IMPORT_POLL_S ≈ 30s ceiling on report publication
_IMPORT_POLL_S = 1.2


class PowerBIError(Exception):
    """A clean, user-facing failure (limits, malformed data)."""


@dataclass(frozen=True)
class PushResult:
    dataset_id: str
    url: str
    rows: int
    columns: int
    updated: bool  # True when an existing dataset was refreshed in place
    # The auto-generated report (reverse-engineered thin PBIX). None when the
    # step is disabled or failed — report_failed distinguishes the two so the
    # tool can say "connector issue, we're on it" only when it actually broke.
    report_url: str | None = None
    report_failed: bool = False


def _dedupe_names(names: list[str]) -> list[str]:
    """Power BI rejects duplicate column names — suffix repeats ("Name (2)")."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        key = name.lower()
        count = seen.get(key, 0) + 1
        seen[key] = count
        out.append(name if count == 1 else f"{name} ({count})")
    return out


def _infer_schema(cols: list[Column]) -> list[dict[str, str]]:
    """Profiled column kinds → Power BI dataTypes. Numbers become Int64 only when
    every parsed value is integral (and exactly float-representable)."""
    names = _dedupe_names([c.name for c in cols])
    schema: list[dict[str, str]] = []
    for name, col in zip(names, cols, strict=True):
        if col.kind == "number":
            nums = [v for v in col.values if isinstance(v, float)]
            integral = all(v.is_integer() and abs(v) < _MAX_INT64 for v in nums)
            data_type = "Int64" if integral else "Double"
        elif col.kind == "date":
            data_type = "DateTime"
        else:  # category / id / text
            data_type = "String"
        schema.append({"name": name, "dataType": data_type})
    return schema


def _serialize_rows(cols: list[Column], schema: list[dict[str, str]]) -> list[dict[str, Any]]:
    """One JSON object per row. Blanks/unparsable cells become null — never ""
    in a number column or "NaN". Dates go out as ISO 8601 (what push datasets
    parse for DateTime)."""
    n_rows = len(cols[0].values) if cols else 0
    rows: list[dict[str, Any]] = []
    for i in range(n_rows):
        row: dict[str, Any] = {}
        for col, spec in zip(cols, schema, strict=True):
            value = col.values[i]
            if col.kind == "number":
                if isinstance(value, float):
                    row[spec["name"]] = int(value) if spec["dataType"] == "Int64" else value
                else:
                    row[spec["name"]] = None
            elif col.kind == "date":
                row[spec["name"]] = (
                    value.strftime("%Y-%m-%dT%H:%M:%S") if isinstance(value, datetime) else None
                )
            else:
                text = str(value).strip() if value is not None else ""
                row[spec["name"]] = text or None
        rows.append(row)
    return rows


class PowerBIService:
    """Push CSV data into the connected user's Power BI as a push dataset."""

    def __init__(self, db: AsyncSession, connectors: Any | None = None) -> None:
        if connectors is None:
            from sila.services.connectors.connector_service import ConnectorService

            connectors = ConnectorService(db)
        self._connectors = connectors

    async def push_csv_dataset(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        filename: str,
        csv_text: str,
        title: str,
        dataset_id: str | None = None,
        extra_charts: list[dict[str, Any]] | None = None,
        charts_authoritative: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> PushResult:
        """Parse → schema → create-or-refresh the dataset → push rows in chunks.

        Raises ValueError (unusable file), PowerBIError (limits), ConnectorError
        (not connected) or NangoError (API failure) — the tool maps each.
        """

        def _note(text: str) -> None:
            if progress is not None:
                progress(text)

        # No row cap here: the caller already bounded the CSV (chat parity —
        # Power BI must aggregate the SAME rows the chat dashboard did).
        header, raw_rows = parse_csv(csv_text, max_rows=1_000_000)  # ValueError propagates
        cols = profile_columns(header, raw_rows)
        if len(cols) > _MAX_COLUMNS:
            raise PowerBIError(
                f"Power BI push datasets support at most {_MAX_COLUMNS} columns; "
                f"{filename} has {len(cols)}."
            )
        schema = _infer_schema(cols)
        rows = _serialize_rows(cols, schema)

        updated = False
        if dataset_id:
            updated = await self._refresh_in_place(
                user_id=user_id, workspace_id=workspace_id, dataset_id=dataset_id, schema=schema
            )
            if updated:
                _note(f"Refreshing the existing '{title}' dataset…")
        if not updated:
            _note(f"Creating the '{title}' dataset in Power BI…")
            dataset_id = await self._create_dataset(
                user_id=user_id, workspace_id=workspace_id, title=title, schema=schema
            )

        sent = 0
        for start in range(0, len(rows), _CHUNK_ROWS):
            chunk = rows[start : start + _CHUNK_ROWS]
            await self._connectors.execute_proxy(
                user_id=user_id,
                workspace_id=workspace_id,
                provider="powerbi",
                method="POST",
                endpoint=f"v1.0/myorg/datasets/{dataset_id}/tables/{_TABLE}/rows",
                data={"rows": chunk},
            )
            sent += len(chunk)
            _note(f"Pushing rows… {sent:,}/{len(rows):,}")

        assert dataset_id is not None  # set on both paths above

        report_url: str | None = None
        report_failed = False
        if settings.powerbi_report_generation:
            _note("Designing the report visuals…")
            try:
                report_url = await self._publish_report(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    dataset_id=dataset_id,
                    title=title,
                    cols=cols,
                    extra_charts=extra_charts,
                    charts_authoritative=charts_authoritative,
                    progress=_note,
                )
            except Exception as exc:  # reverse-engineered format — NEVER fail the push
                report_failed = True
                log.warning(
                    "powerbi.report_generation_failed",
                    error=str(exc)[:400],
                    error_type=type(exc).__name__,
                )

        return PushResult(
            dataset_id=dataset_id,
            url=_DATASET_URL.format(id=dataset_id),
            rows=len(rows),
            columns=len(schema),
            updated=updated,
            report_url=report_url,
            report_failed=report_failed,
        )

    async def _publish_report(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        dataset_id: str,
        title: str,
        cols: list[Column],
        extra_charts: list[dict[str, Any]] | None,
        charts_authoritative: bool,
        progress: Callable[[str], None],
    ) -> str:
        """Build the thin PBIX, import it, poll to completion, rebind.

        ⚠ Rides the reverse-engineered Layout format (see powerbi_report.py) —
        the caller treats ANY exception as "report step unavailable" and falls
        back to the dataset link; the pushed data is already safe."""
        from sila.services.connectors.powerbi_report import build_thin_pbix

        pbix = build_thin_pbix(
            dataset_id, title, cols, extra_charts, authoritative=charts_authoritative
        )
        boundary = f"silapbix{secrets.token_hex(8)}"
        body = (
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{title}.pbix"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + pbix
            + f"\r\n--{boundary}--\r\n".encode()
        )
        progress("Publishing the report to Power BI…")
        # Nango's proxy is JSON-only (it 400s multipart — the same limitation
        # that parked the Drive upload), so this ONE call goes direct: token
        # fetched in-process, host hardcoded, everything else stays proxied.
        token = await self._connectors.fetch_access_token(
            user_id=user_id, workspace_id=workspace_id, provider="powerbi"
        )
        resp = await self._upload_pbix(
            token, title, body, f"multipart/form-data; boundary={boundary}"
        )
        import_id = str(resp.get("id") or "")
        if not import_id:
            raise PowerBIError(f"import accepted but returned no id: {resp}")

        report_id, web_url = "", ""
        for _ in range(_IMPORT_POLLS):
            await asyncio.sleep(_IMPORT_POLL_S)
            status = await self._connectors.execute_proxy(
                user_id=user_id,
                workspace_id=workspace_id,
                provider="powerbi",
                method="GET",
                endpoint=f"v1.0/myorg/imports/{import_id}",
            )
            state = str(status.get("importState") or "")
            if state == "Succeeded":
                reports = status.get("reports") or []
                if reports and isinstance(reports[0], dict):
                    report_id = str(reports[0].get("id") or "")
                    web_url = str(reports[0].get("webUrl") or "")
                break
            if state == "Failed":
                raise PowerBIError(f"import failed: {status.get('error') or status}")
        if not report_id:
            raise PowerBIError("import did not finish in time")

        # Best-effort: make the live connection authoritative (the Connections
        # part inside the pbix carries best-guess values).
        # (rebind below rides the normal proxy — JSON body, no escape hatch)
        try:
            await self._connectors.execute_proxy(
                user_id=user_id,
                workspace_id=workspace_id,
                provider="powerbi",
                method="POST",
                endpoint=f"v1.0/myorg/reports/{report_id}/Rebind",
                data={"datasetId": dataset_id},
            )
        except Exception as exc:
            log.info("powerbi.rebind_skipped", error=str(exc)[:200])

        return web_url or f"https://app.powerbi.com/groups/me/reports/{report_id}"

    async def _upload_pbix(
        self, token: str, title: str, body: bytes, content_type: str
    ) -> dict[str, Any]:
        """Direct multipart POST to api.powerbi.com (Nango's proxy can't carry
        multipart). Host hardcoded — never user input; the token lives only in
        this call. Raises PowerBIError with the provider's message on reject."""
        import httpx

        url = (
            "https://api.powerbi.com/v1.0/myorg/imports"
            f"?datasetDisplayName={quote(title)}.pbix&nameConflict=CreateOrOverwrite"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
                content=body,
            )
        if resp.status_code >= 300:
            raise PowerBIError(f"import upload failed ({resp.status_code}): {resp.text[:300]}")
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    async def _create_dataset(
        self, *, user_id: uuid.UUID, workspace_id: uuid.UUID, title: str, schema: list[dict]
    ) -> str:
        resp = await self._connectors.execute_proxy(
            user_id=user_id,
            workspace_id=workspace_id,
            provider="powerbi",
            method="POST",
            endpoint="v1.0/myorg/datasets",
            data={
                "name": title,
                "defaultMode": "Push",
                "tables": [{"name": _TABLE, "columns": schema}],
            },
        )
        dataset_id = str(resp.get("id") or "")
        if not dataset_id:
            raise PowerBIError("Power BI created the dataset but returned no id.")
        return dataset_id

    async def _refresh_in_place(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        dataset_id: str,
        schema: list[dict],
    ) -> bool:
        """Point the existing dataset at the (possibly changed) schema and clear
        its rows. False when the dataset is gone (deleted in the Power BI UI) —
        the caller then creates a fresh one instead of failing the turn."""
        from sila.adapters.connectors.nango_client import NangoError

        try:
            await self._connectors.execute_proxy(
                user_id=user_id,
                workspace_id=workspace_id,
                provider="powerbi",
                method="PUT",
                endpoint=f"v1.0/myorg/datasets/{dataset_id}/tables/{_TABLE}",
                data={"name": _TABLE, "columns": schema},
            )
            await self._connectors.execute_proxy(
                user_id=user_id,
                workspace_id=workspace_id,
                provider="powerbi",
                method="DELETE",
                endpoint=f"v1.0/myorg/datasets/{dataset_id}/tables/{_TABLE}/rows",
            )
        except NangoError:
            return False
        return True


__all__ = ["PowerBIError", "PowerBIService", "PushResult"]
