"""Data access for VettingRun and VettingFinding."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sila.db.models import VettingFinding, VettingRun


def _now_naive() -> datetime:
    """UTC now, tz-naive. Matches the rest of the codebase's datetime conventions."""
    return datetime.utcnow()


class VettingRepository:
    """O(log n) via (agent_id, started_at DESC) index."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_run(
        self,
        *,
        agent_id: uuid.UUID,
        workspace_id: uuid.UUID,
        agent_version_id: uuid.UUID | None,
        classification: str,
        triggered_by: uuid.UUID,
    ) -> VettingRun:
        run = VettingRun(
            agent_id=agent_id,
            workspace_id=workspace_id,
            agent_version_id=agent_version_id,
            classification=classification,
            triggered_by=triggered_by,
            status="scanning",
        )
        self._db.add(run)
        await self._db.flush()
        return run

    async def get_run(self, run_id: uuid.UUID) -> VettingRun | None:
        return await self._db.get(VettingRun, run_id)

    async def list_for_agent(self, agent_id: uuid.UUID) -> list[VettingRun]:
        result = await self._db.execute(
            select(VettingRun)
            .where(VettingRun.agent_id == agent_id)
            .order_by(VettingRun.started_at.desc())
        )
        return list(result.scalars().all())

    async def latest_for_agent(self, agent_id: uuid.UUID) -> VettingRun | None:
        result = await self._db.execute(
            select(VettingRun)
            .where(VettingRun.agent_id == agent_id)
            .order_by(VettingRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def complete_run(
        self,
        run: VettingRun,
        *,
        outcome: str,
        finding_summary: dict[str, Any],
    ) -> VettingRun:
        run.status = "completed"
        run.outcome = outcome
        run.finding_summary = finding_summary
        run.completed_at = _now_naive()
        await self._db.flush()
        return run

    async def fail_run(self, run: VettingRun, error: str) -> VettingRun:
        run.status = "failed"
        run.outcome = "error"
        run.finding_summary = {"error": error}
        run.completed_at = _now_naive()
        await self._db.flush()
        return run

    async def record_decision(
        self,
        run: VettingRun,
        *,
        approved_by: uuid.UUID,
        decision: str,
        note: str | None,
    ) -> VettingRun:
        run.approved_by = approved_by
        run.approval_decision = decision
        run.approval_note = note
        await self._db.flush()
        return run

    async def add_findings(
        self,
        *,
        run_id: uuid.UUID,
        workspace_id: uuid.UUID,
        findings: list[Any],
    ) -> int:
        """Bulk-insert findings. Returns count written. O(k) inserts."""
        rows = [
            VettingFinding(
                vetting_run_id=run_id,
                workspace_id=workspace_id,
                # Defensive truncation to the column limits — a long finding field must
                # NEVER raise and roll back the build transaction it shares (get_db).
                finding_type=f.finding_type[:100],
                severity=f.severity[:20],
                location=f.location[:255],
                matched_preview=f.matched_preview,
                matched_full=f.matched_full,
                finding_metadata=f.metadata or {},
            )
            for f in findings
        ]
        for r in rows:
            self._db.add(r)
        await self._db.flush()
        return len(rows)

    async def list_findings(
        self, run_id: uuid.UUID, *, include_full: bool = False
    ) -> list[VettingFinding]:
        result = await self._db.execute(
            select(VettingFinding)
            .where(VettingFinding.vetting_run_id == run_id)
            .order_by(VettingFinding.created_at.asc())
        )
        items = list(result.scalars().all())
        if not include_full:
            for f in items:
                f.matched_full = None
        return items
