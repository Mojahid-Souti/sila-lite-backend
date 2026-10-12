"""Read-only dashboard aggregations.

All queries are workspace-scoped (the API layer enforces role; this layer
takes workspace_id as a required argument). Materialized view
`mv_workspace_daily` is refreshed every 60s by a background task.
Per-agent and timeseries queries hit `mv_workspace_daily` when possible,
falling back to the raw tables for the per-agent slice.

Algorithmic complexity:
  - KPI summary: O(d) where d = days in window (typ. 7 or 30) — single MV scan
  - Timeseries: O(d) — single MV scan
  - Top-N agents: O(n log n) over agents touched in window — small (~< 100)
  - Per-agent metrics: O(r) where r = api_request_log rows for that agent in window
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import case, desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sila.core.policies.analytics_policies import (
    ComputeCostPolicy,
    HoursSavedPolicy,
    get_compute_cost_policy,
    get_hours_saved_policy,
)
from sila.db.models import Agent, ApiRequestLog


@dataclass(frozen=True)
class KpiSummary:
    invocations: int
    hours_saved: float
    active_agents: int
    p95_latency_ms: int
    tool_calls: int
    vetting_runs_completed: int
    documents_uploaded: int
    range_days: int


@dataclass(frozen=True)
class TimeseriesPoint:
    day: date
    invocations: int
    tool_calls: int
    avg_latency_ms: int


@dataclass(frozen=True)
class AgentLeaderboardRow:
    agent_id: uuid.UUID
    name: str
    invocations: int
    success_rate: float
    p95_latency_ms: int
    hours_saved: float


@dataclass(frozen=True)
class ComputeCostSummary:
    total_tokens: int
    input_tokens: int  # G2 — priced separately from output
    output_tokens: int
    reasoning_tokens: int  # billed as output, never shown to the user
    runs: int
    sandbox_runs: int  # G1 — the agent.test split
    production_runs: int  # G1 — the public.invoke split
    active_seconds: float  # G4 — prefill + decode (GPU busy), NOT wall clock
    prefill_seconds: float
    decode_seconds: float
    wall_seconds: float  # kept for responsiveness reporting, never for cost
    # Discontinuity guards: rows written before the v2 schema carry no GPU
    # timing, so they are counted for TOKENS but excluded from active_seconds.
    legacy_runs: int
    gpu_timing_coverage_pct: float
    utilization_pct: float  # active inference time / calendar time
    marginal_usd_per_1m: float  # cost/1M at the measured throughput (GPU busy)
    effective_usd_per_1m: float  # honest cost: amortized hardware + energy / tokens
    compute_cost_usd: float  # the period's local cost (hardware amortization + energy)
    energy_usd: float
    warm_idle_energy_usd: float  # keep-alive residency (see docstring: ASSUMPTION)
    hardware_usd: float
    cloud_equivalent_usd: float
    savings_vs_cloud_usd: float
    cloud_reference_name: str
    currency: str
    range_days: int


@dataclass(frozen=True)
class AgentCostRow:
    """Per-agent cost (G3) — what the Bazaar revenue share needs."""

    agent_id: str
    agent_name: str
    runs: int
    input_tokens: int
    output_tokens: int
    active_seconds: float
    compute_cost_usd: float


class AnalyticsService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        hours_saved: HoursSavedPolicy | None = None,
        cost: ComputeCostPolicy | None = None,
    ) -> None:
        self._db = db
        self._hours_saved = hours_saved or get_hours_saved_policy()
        self._cost = cost or get_compute_cost_policy()

    # ------------------------------------------------------------------
    # KPI summary
    # ------------------------------------------------------------------

    async def kpis(self, *, workspace_id: uuid.UUID, range_days: int = 7) -> KpiSummary:
        since = date.today() - timedelta(days=range_days - 1)

        # One MV scan: SUM the counts and the p95 (which is already aggregated daily).
        # The MAX across days approximates a windowed p95 — fine for an exec-tier KPI;
        # finer breakdowns use the timeseries.
        row = (
            await self._db.execute(
                text(
                    """
                    SELECT
                      COALESCE(SUM(invocations), 0) AS invocations,
                      COALESCE(SUM(tool_calls), 0) AS tool_calls,
                      COALESCE(SUM(vetting_runs_completed), 0) AS vetting_runs_completed,
                      COALESCE(SUM(documents_uploaded), 0) AS documents_uploaded,
                      COALESCE(MAX(p95_latency_ms), 0) AS p95_latency_ms
                    FROM mv_workspace_daily
                    WHERE workspace_id = :wid AND day >= :since
                    """
                ),
                {"wid": str(workspace_id), "since": since},
            )
        ).one()

        # Active agents = agents that were invoked in the window.
        active_agents = (
            await self._db.execute(
                select(func.count(func.distinct(ApiRequestLog.agent_id))).where(
                    ApiRequestLog.workspace_id == workspace_id,
                    ApiRequestLog.agent_id.is_not(None),
                    ApiRequestLog.ts >= since,
                )
            )
        ).scalar_one()

        hours_saved = await self._hours_saved_for_window(
            workspace_id=workspace_id, range_days=range_days
        )

        return KpiSummary(
            invocations=int(row.invocations),
            hours_saved=round(hours_saved, 1),
            active_agents=int(active_agents or 0),
            p95_latency_ms=int(row.p95_latency_ms),
            tool_calls=int(row.tool_calls),
            vetting_runs_completed=int(row.vetting_runs_completed),
            documents_uploaded=int(row.documents_uploaded),
            range_days=range_days,
        )

    # ------------------------------------------------------------------
    # Compute cost (local inference)
    # ------------------------------------------------------------------

    # Actions that represent a real inference run. Sandbox tests and production
    # invocations are BOTH counted (G1) but stay distinguishable by action.
    _COST_ACTIONS = ("agent.test", "public.invoke")

    # ASSUMPTION: OLLAMA_KEEP_ALIVE=5m — after each run the weights stay resident
    # (measured 14 W, 9,990 MiB) until this expires. Charged at warm-idle power.
    # Capped at calendar time and NOT deduplicated for overlapping runs, so it is
    # an upper bound; it is reported as its own field so it can be inspected or
    # zeroed rather than silently folded into the total.
    _KEEP_ALIVE_SECONDS = 300.0

    async def compute_cost(
        self, *, workspace_id: uuid.UUID, range_days: int = 30
    ) -> ComputeCostSummary:
        """Cost of local inference: amortized hardware + electricity.

        Sources every figure from the v2 cost-telemetry block written by
        `core.cost_telemetry` (see that module for the four defects this
        addresses). In particular:

        G1  counts BOTH `agent.test` (sandbox) and `public.invoke` (production);
            the split is returned so they remain separable.
        G2  input/output/reasoning tokens are summed separately.
        G4  `active_seconds` = prefill + decode (GPU actually busy). Wall clock
            is summed separately as `wall_seconds` and never used for cost.

        DISCONTINUITY: rows written before the v2 schema have no prefill/decode
        fields. They still contribute TOKENS (so volume stays comparable) but
        contribute NO active_seconds, and are counted in `legacy_runs` with a
        `gpu_timing_coverage_pct`. Utilization/marginal figures computed over a
        window containing legacy rows are therefore understated on GPU time —
        that is why coverage is surfaced rather than hidden.

        O(r) over cost rows in the window (indexed on timestamp/workspace).
        """
        since = datetime.now(UTC) - timedelta(days=range_days)
        row = (
            await self._db.execute(
                text(
                    "SELECT "
                    # Tokens: prefer the v2 split, fall back to the v1 blended
                    # `tokens` so historical volume is not lost.
                    "COALESCE(SUM((metadata->>'input_tokens')::bigint), 0) AS input_tokens, "
                    "COALESCE(SUM((metadata->>'output_tokens')::bigint), 0) AS output_tokens, "
                    "COALESCE(SUM((metadata->>'reasoning_tokens')::bigint), 0) "
                    "  AS reasoning_tokens, "
                    "COALESCE(SUM((metadata->>'tokens')::bigint), 0) AS total_tokens, "
                    # G4: real GPU time, only from rows that actually carry it.
                    "COALESCE(SUM((metadata->>'prefill_seconds')::double precision), 0) "
                    "  AS prefill_seconds, "
                    "COALESCE(SUM((metadata->>'decode_seconds')::double precision), 0) "
                    "  AS decode_seconds, "
                    "COALESCE(SUM((metadata->>'latency_ms')::double precision), 0) AS latency_ms, "
                    "COUNT(*) AS runs, "
                    "COUNT(*) FILTER (WHERE action = 'agent.test') AS sandbox_runs, "
                    "COUNT(*) FILTER (WHERE action = 'public.invoke') AS production_runs, "
                    "COUNT(*) FILTER (WHERE metadata->>'active_seconds' IS NULL) AS legacy_runs "
                    "FROM audit_log "
                    "WHERE action = ANY(:actions) AND outcome = 'success' "
                    "AND actor_workspace_id = :wid AND timestamp >= :since"
                ),
                {"wid": workspace_id, "since": since, "actions": list(self._COST_ACTIONS)},
            )
        ).one()

        input_tokens = int(row.input_tokens or 0)
        output_tokens = int(row.output_tokens or 0)
        reasoning_tokens = int(row.reasoning_tokens or 0)
        # v1 rows only have the blended figure; use whichever is larger so a
        # mixed window neither double-counts nor loses legacy volume.
        total_tokens = max(int(row.total_tokens or 0), input_tokens + output_tokens)
        prefill_seconds = float(row.prefill_seconds or 0)
        decode_seconds = float(row.decode_seconds or 0)
        active_seconds = prefill_seconds + decode_seconds  # G4
        wall_seconds = float(row.latency_ms or 0) / 1000.0
        runs = int(row.runs or 0)
        legacy_runs = int(row.legacy_runs or 0)
        coverage = ((runs - legacy_runs) / runs * 100) if runs else 0.0

        p = self._cost
        calendar_hours = range_days * 24
        # Energy: prefill and decode are billed at their OWN measured power
        # states (82 W vs 56 W) instead of one blended number.
        energy_usd = (
            prefill_seconds / 3600.0 * p.prefill_power_kw
            + decode_seconds / 3600.0 * p.decode_power_kw
        ) * p.electricity_usd_per_kwh
        # Keep-alive residency (see _KEEP_ALIVE_SECONDS — an ASSUMPTION).
        warm_idle_seconds = min(runs * self._KEEP_ALIVE_SECONDS, calendar_hours * 3600.0)
        warm_idle_energy_usd = (
            warm_idle_seconds / 3600.0 * p.warm_idle_power_kw * p.electricity_usd_per_kwh
        )
        # Hardware amortizes over the whole calendar window (it depreciates
        # idle) — that's why low utilization makes effective cost-per-token high.
        hardware_usd = p.hardware_usd_per_hour * calendar_hours * p.ai_allocation_fraction
        compute_cost_usd = energy_usd + warm_idle_energy_usd + hardware_usd

        effective_per_1m = (compute_cost_usd / total_tokens * 1_000_000) if total_tokens else 0.0
        if active_seconds > 0 and total_tokens > 0:
            throughput_per_hour = total_tokens / (active_seconds / 3600.0)
            marginal_per_1m = (
                (p.hardware_usd_per_hour + p.decode_power_kw * p.electricity_usd_per_kwh)
                / throughput_per_hour
                * 1_000_000
            )
        else:
            marginal_per_1m = 0.0
        utilization_pct = (
            active_seconds / (calendar_hours * 3600.0) * 100 if calendar_hours else 0.0
        )
        # Cloud reference now prices input and output at their own rates (G2).
        cloud_equivalent_usd = (
            input_tokens / 1_000_000 * p.cloud_reference_input_usd_per_1m
            + output_tokens / 1_000_000 * p.cloud_reference_output_usd_per_1m
        )
        if not (input_tokens or output_tokens):  # legacy-only window
            cloud_equivalent_usd = total_tokens / 1_000_000 * p.cloud_reference_usd_per_1m

        conv = p.usd_to_report_currency
        return ComputeCostSummary(
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            runs=runs,
            sandbox_runs=int(row.sandbox_runs or 0),
            production_runs=int(row.production_runs or 0),
            active_seconds=round(active_seconds, 3),
            prefill_seconds=round(prefill_seconds, 3),
            decode_seconds=round(decode_seconds, 3),
            wall_seconds=round(wall_seconds, 3),
            legacy_runs=legacy_runs,
            gpu_timing_coverage_pct=round(coverage, 1),
            utilization_pct=round(utilization_pct, 4),
            marginal_usd_per_1m=round(conv(marginal_per_1m), 4),
            effective_usd_per_1m=round(conv(effective_per_1m), 4),
            compute_cost_usd=round(conv(compute_cost_usd), 4),
            energy_usd=round(conv(energy_usd), 4),
            warm_idle_energy_usd=round(conv(warm_idle_energy_usd), 4),
            hardware_usd=round(conv(hardware_usd), 4),
            cloud_equivalent_usd=round(conv(cloud_equivalent_usd), 4),
            savings_vs_cloud_usd=round(conv(cloud_equivalent_usd - compute_cost_usd), 4),
            cloud_reference_name=p.cloud_reference_name,
            currency=p.report_in.upper(),
            range_days=range_days,
        )

    async def cost_by_agent(
        self, *, workspace_id: uuid.UUID, range_days: int = 30, limit: int = 50
    ) -> list[AgentCostRow]:
        """G3 — per-agent cost, which workspace-level totals cannot produce and
        the Bazaar revenue share requires. Groups on the `agent_id` recorded in
        the telemetry block, joining agents for the display name.

        Energy is billed at the measured prefill/decode power states; hardware
        amortization is deliberately NOT divided per agent here (it is a
        window-level sunk cost — apportioning it would invent a rule the policy
        does not state). This figure is therefore MARGINAL cost per agent.
        O(r) over cost rows, one GROUP BY."""
        since = datetime.now(UTC) - timedelta(days=range_days)
        rows = (
            await self._db.execute(
                text(
                    "SELECT metadata->>'agent_id' AS agent_id, "
                    "COUNT(*) AS runs, "
                    "COALESCE(SUM((metadata->>'input_tokens')::bigint), 0) AS input_tokens, "
                    "COALESCE(SUM((metadata->>'output_tokens')::bigint), 0) AS output_tokens, "
                    "COALESCE(SUM((metadata->>'prefill_seconds')::double precision), 0) AS pf, "
                    "COALESCE(SUM((metadata->>'decode_seconds')::double precision), 0) AS dc "
                    "FROM audit_log "
                    "WHERE action = ANY(:actions) AND outcome = 'success' "
                    "AND actor_workspace_id = :wid AND timestamp >= :since "
                    "AND metadata->>'agent_id' IS NOT NULL "
                    "GROUP BY metadata->>'agent_id' "
                    "ORDER BY SUM((metadata->>'output_tokens')::bigint) DESC NULLS LAST "
                    "LIMIT :limit"
                ),
                {
                    "wid": workspace_id,
                    "since": since,
                    "actions": list(self._COST_ACTIONS),
                    "limit": limit,
                },
            )
        ).all()
        if not rows:
            return []
        names = dict(
            (
                await self._db.execute(
                    select(Agent.id, Agent.name).where(
                        Agent.id.in_([uuid.UUID(r.agent_id) for r in rows])
                    )
                )
            ).all()
        )
        p = self._cost
        out: list[AgentCostRow] = []
        for r in rows:
            pf, dc = float(r.pf or 0), float(r.dc or 0)
            energy = (
                pf / 3600.0 * p.prefill_power_kw + dc / 3600.0 * p.decode_power_kw
            ) * p.electricity_usd_per_kwh
            out.append(
                AgentCostRow(
                    agent_id=str(r.agent_id),
                    agent_name=str(names.get(uuid.UUID(r.agent_id), "(deleted)")),
                    runs=int(r.runs or 0),
                    input_tokens=int(r.input_tokens or 0),
                    output_tokens=int(r.output_tokens or 0),
                    active_seconds=round(pf + dc, 3),
                    compute_cost_usd=round(p.usd_to_report_currency(energy), 6),
                )
            )
        return out

    # ------------------------------------------------------------------
    # Timeseries (for chart)
    # ------------------------------------------------------------------

    async def timeseries(
        self, *, workspace_id: uuid.UUID, range_days: int = 30
    ) -> list[TimeseriesPoint]:
        since = date.today() - timedelta(days=range_days - 1)
        rows = (
            await self._db.execute(
                text(
                    """
                    SELECT day, invocations, tool_calls, avg_latency_ms
                    FROM mv_workspace_daily
                    WHERE workspace_id = :wid AND day >= :since
                    ORDER BY day ASC
                    """
                ),
                {"wid": str(workspace_id), "since": since},
            )
        ).all()
        return [
            TimeseriesPoint(
                day=r.day,
                invocations=int(r.invocations),
                tool_calls=int(r.tool_calls),
                avg_latency_ms=int(r.avg_latency_ms),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Top-N agents
    # ------------------------------------------------------------------

    async def top_agents(
        self, *, workspace_id: uuid.UUID, range_days: int = 7, limit: int = 10
    ) -> list[AgentLeaderboardRow]:
        since = datetime.utcnow() - timedelta(days=range_days)
        # Aggregate by agent from api_request_log.
        success_case = case((ApiRequestLog.status_code.between(200, 299), 1), else_=0)
        result = await self._db.execute(
            select(
                ApiRequestLog.agent_id,
                func.count().label("invocations"),
                func.sum(success_case).label("successes"),
                func.percentile_cont(0.95)
                .within_group(ApiRequestLog.latency_ms)
                .label("p95_latency_ms"),
            )
            .where(
                ApiRequestLog.workspace_id == workspace_id,
                ApiRequestLog.agent_id.is_not(None),
                ApiRequestLog.ts >= since,
            )
            .group_by(ApiRequestLog.agent_id)
            .order_by(desc("invocations"))
            .limit(limit)
        )
        rows = list(result.all())
        if not rows:
            return []

        # Resolve agent names in a single query
        agent_ids = [r.agent_id for r in rows]
        agents = await self._db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))
        name_by_id = {a.id: a.name for a in agents.all()}

        out: list[AgentLeaderboardRow] = []
        for r in rows:
            invocations = int(r.invocations or 0)
            successes = int(r.successes or 0)
            success_rate = (successes / invocations) if invocations else 0.0
            mins = self._hours_saved.minutes_for(
                agent_id=str(r.agent_id), agent_name=name_by_id.get(r.agent_id, "")
            )
            out.append(
                AgentLeaderboardRow(
                    agent_id=r.agent_id,
                    name=name_by_id.get(r.agent_id, "(unknown)"),
                    invocations=invocations,
                    success_rate=round(success_rate, 3),
                    p95_latency_ms=int(r.p95_latency_ms or 0),
                    hours_saved=round(invocations * mins / 60, 1),
                )
            )
        return out

    # ------------------------------------------------------------------
    # Hours-saved (windowed total)
    # ------------------------------------------------------------------

    async def _hours_saved_for_window(self, *, workspace_id: uuid.UUID, range_days: int) -> float:
        since = datetime.utcnow() - timedelta(days=range_days)
        # Get per-agent invocation counts in window
        result = await self._db.execute(
            select(
                ApiRequestLog.agent_id,
                func.count().label("invocations"),
            )
            .where(
                ApiRequestLog.workspace_id == workspace_id,
                ApiRequestLog.agent_id.is_not(None),
                ApiRequestLog.ts >= since,
            )
            .group_by(ApiRequestLog.agent_id)
        )
        rows = list(result.all())
        if not rows:
            return 0.0
        # Resolve names
        agent_ids = [r.agent_id for r in rows]
        agents = await self._db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))
        name_by_id = {a.id: a.name for a in agents.all()}
        total_minutes = 0
        for r in rows:
            mins = self._hours_saved.minutes_for(
                agent_id=str(r.agent_id), agent_name=name_by_id.get(r.agent_id, "")
            )
            total_minutes += int(r.invocations or 0) * mins
        return total_minutes / 60.0

    # ------------------------------------------------------------------
    # Audit log search (admin only at API layer)
    # ------------------------------------------------------------------

    async def search_audit_log(
        self,
        *,
        workspace_id: uuid.UUID,
        action: str | None = None,
        actor_user_id: uuid.UUID | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        from sila.db.models import AuditLog

        q = select(AuditLog).where(AuditLog.actor_workspace_id == workspace_id)
        if action:
            q = q.where(AuditLog.action == action)
        if actor_user_id:
            q = q.where(AuditLog.actor_user_id == actor_user_id)
        if from_dt:
            q = q.where(AuditLog.timestamp >= from_dt)
        if to_dt:
            q = q.where(AuditLog.timestamp <= to_dt)

        count_q = select(func.count()).select_from(q.subquery())
        total = int((await self._db.execute(count_q)).scalar_one())

        page = max(page, 1)
        size = min(max(size, 1), 200)
        items_q = q.order_by(AuditLog.timestamp.desc()).offset((page - 1) * size).limit(size)
        items = list((await self._db.execute(items_q)).scalars().all())
        return [
            {
                "id": str(a.id),
                "timestamp": a.timestamp.isoformat(),
                "actor_user_id": str(a.actor_user_id) if a.actor_user_id else None,
                "action": a.action,
                "resource_type": a.resource_type,
                "resource_id": str(a.resource_id) if a.resource_id else None,
                "outcome": a.outcome,
                "metadata": a.event_metadata or {},
            }
            for a in items
        ], total
