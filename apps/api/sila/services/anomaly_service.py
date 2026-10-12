"""On-read anomaly detection: z-score over a rolling baseline.

Rules come from `infra/policies/anomalies.yaml`. Each rule names a metric
in `mv_workspace_daily` and a threshold. On every Command Center load we
re-evaluate. The first time a threshold is crossed in a given window we
persist an `anomaly_alerts` row so it survives page reloads and can be
acknowledged. Subsequent reads with the same window reuse the existing row.

Algorithm — z-score:
  z = (today_value - mean(baseline)) / stddev(baseline)
  fire if z > threshold AND len(baseline) >= min_sample_size

Complexity: O(d × r) where d = baseline_days (<= 7) and r = rule count (<= 10).
Effectively constant per call.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sila.core.policies.analytics_policies import (
    AnomalyPolicy,
    AnomalyRule,
    get_anomaly_policy,
)
from sila.db.models import AnomalyAlert


@dataclass(frozen=True)
class EvaluatedRule:
    rule: AnomalyRule
    observed_value: float
    fired: bool
    z_score: float | None  # None for absolute-kind rules


class AnomalyService:
    def __init__(self, db: AsyncSession, *, policy: AnomalyPolicy | None = None) -> None:
        self._db = db
        self._policy = policy or get_anomaly_policy()

    async def evaluate_and_persist(self, *, workspace_id: uuid.UUID) -> list[EvaluatedRule]:
        """Run each rule against the latest day's MV row. Persist new alerts."""
        out: list[EvaluatedRule] = []
        for rule in self._policy.rules:
            try:
                evaluated = await self._evaluate_rule(workspace_id, rule)
            except Exception:  # noqa: BLE001 — anomaly check failure must not break the dashboard
                continue
            out.append(evaluated)
            if evaluated.fired:
                await self._persist_if_new(workspace_id, rule, evaluated.observed_value)
        return out

    async def list_open(self, workspace_id: uuid.UUID) -> list[AnomalyAlert]:
        result = await self._db.execute(
            select(AnomalyAlert)
            .where(
                AnomalyAlert.workspace_id == workspace_id,
                AnomalyAlert.status == "open",
            )
            .order_by(AnomalyAlert.created_at.desc())
        )
        return list(result.scalars().all())

    async def acknowledge(
        self,
        *,
        alert_id: uuid.UUID,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> AnomalyAlert | None:
        alert = await self._db.get(AnomalyAlert, alert_id)
        if not alert or alert.workspace_id != workspace_id:
            return None
        async with self._db.begin_nested():
            alert.status = "acknowledged"
            alert.acknowledged_by = actor_id
            alert.acknowledged_at = datetime.utcnow()
            await self._db.flush()
        return alert

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _evaluate_rule(self, workspace_id: uuid.UUID, rule: AnomalyRule) -> EvaluatedRule:
        """Read the metric series for this rule and apply the threshold."""
        if rule.kind == "absolute":
            return await self._evaluate_absolute(workspace_id, rule)
        return await self._evaluate_zscore(workspace_id, rule)

    async def _evaluate_zscore(self, workspace_id: uuid.UUID, rule: AnomalyRule) -> EvaluatedRule:
        series = await self._series(workspace_id, rule.metric, rule.baseline_days + 1)
        if len(series) < rule.min_sample_size + 1:
            return EvaluatedRule(rule=rule, observed_value=0.0, fired=False, z_score=None)
        baseline = [v for _, v in series[:-1]]
        today_value = float(series[-1][1])
        mean = statistics.fmean(baseline)
        stdev = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0
        if stdev == 0:
            # Flat baseline — only fire if today is meaningfully nonzero AND > mean
            fired = today_value > mean and today_value > 0
            z = float("inf") if fired else 0.0
        else:
            z = (today_value - mean) / stdev
            fired = z > rule.threshold
        return EvaluatedRule(rule=rule, observed_value=today_value, fired=fired, z_score=z)

    async def _evaluate_absolute(self, workspace_id: uuid.UUID, rule: AnomalyRule) -> EvaluatedRule:
        # 'tool_failure_rate' is a derived metric — compute from raw rates.
        if rule.metric == "tool_failure_rate":
            today_rate, sample_size = await self._tool_failure_rate_today(workspace_id)
            if sample_size < rule.min_sample_size:
                return EvaluatedRule(
                    rule=rule, observed_value=today_rate, fired=False, z_score=None
                )
            return EvaluatedRule(
                rule=rule,
                observed_value=today_rate,
                fired=today_rate > rule.threshold,
                z_score=None,
            )
        # Fall back to single-day MV value
        series = await self._series(workspace_id, rule.metric, 1)
        if not series:
            return EvaluatedRule(rule=rule, observed_value=0.0, fired=False, z_score=None)
        today_value = float(series[-1][1])
        return EvaluatedRule(
            rule=rule,
            observed_value=today_value,
            fired=today_value > rule.threshold,
            z_score=None,
        )

    async def _series(
        self, workspace_id: uuid.UUID, metric: str, days: int
    ) -> list[tuple[datetime, float]]:
        # Whitelist the column name to avoid SQL injection via metric.
        if metric not in {
            "invocations",
            "tool_calls",
            "vetting_runs_completed",
            "documents_uploaded",
            "agent_tests",
            "p95_latency_ms",
            "avg_latency_ms",
        }:
            return []
        since = (datetime.utcnow() - timedelta(days=days)).date()
        rows = (
            await self._db.execute(
                text(
                    f"""
                    SELECT day, {metric} AS value
                    FROM mv_workspace_daily
                    WHERE workspace_id = :wid AND day >= :since
                    ORDER BY day ASC
                    """
                ),
                {"wid": str(workspace_id), "since": since},
            )
        ).all()
        return [(r.day, float(r.value or 0)) for r in rows]

    async def _tool_failure_rate_today(self, workspace_id: uuid.UUID) -> tuple[float, int]:
        row = (
            await self._db.execute(
                text(
                    """
                    SELECT
                      COALESCE(SUM(tool_calls), 0) AS total,
                      COALESCE(SUM(tool_calls) - SUM(tool_success), 0) AS failures
                    FROM mv_workspace_daily
                    WHERE workspace_id = :wid AND day = current_date
                    """
                ),
                {"wid": str(workspace_id)},
            )
        ).one()
        total = int(row.total or 0)
        failures = int(row.failures or 0)
        if total == 0:
            return 0.0, 0
        return failures / total, total

    async def _persist_if_new(
        self,
        workspace_id: uuid.UUID,
        rule: AnomalyRule,
        observed: float,
    ) -> None:
        """Avoid duplicate alerts for the same rule + same day window."""
        now = datetime.utcnow()
        window_start = datetime.combine(now.date(), datetime.min.time())
        existing = await self._db.execute(
            select(AnomalyAlert.id).where(
                AnomalyAlert.workspace_id == workspace_id,
                AnomalyAlert.metric_name == rule.metric,
                AnomalyAlert.window_start == window_start,
            )
        )
        if existing.first():
            return
        alert = AnomalyAlert(
            workspace_id=workspace_id,
            metric_name=rule.metric,
            threshold_kind=rule.kind,
            threshold_value=rule.threshold,
            observed_value=observed,
            window_start=window_start,
            window_end=now,
            alert_metadata={"rule_id": rule.id, "severity": rule.severity, "note": rule.note},
        )
        self._db.add(alert)
        await self._db.flush()
