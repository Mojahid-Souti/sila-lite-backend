"""Phase 8 — Command Center & analytics

- anomaly_alerts: rows recorded by the anomaly service when a metric crosses
  its policy threshold. Acknowledged by workspace admins.
- mv_workspace_daily: materialized view that rolls up daily counts from
  api_request_log, tool_invocations, vetting_runs, and audit_log. Powers the
  KPI strip and timeseries chart. Refreshed every 60s by a background task
  in the API lifespan. `CONCURRENTLY` requires a UNIQUE index on the MV.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # anomaly_alerts
    # ------------------------------------------------------------------
    op.create_table(
        "anomaly_alerts",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric_name", sa.String(80), nullable=False),
        sa.Column("threshold_kind", sa.String(20), nullable=False),
        sa.Column("threshold_value", sa.Numeric, nullable=False),
        sa.Column("observed_value", sa.Numeric, nullable=False),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column(
            "acknowledged_by",
            UUID(as_uuid=True),
            sa.ForeignKey("auth.users.id"),
            nullable=True,
        ),
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("alert_metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('open','acknowledged','resolved')",
            name="anomaly_alert_status",
        ),
        sa.CheckConstraint(
            "threshold_kind IN ('zscore','absolute')",
            name="anomaly_threshold_kind",
        ),
    )
    op.create_index(
        "ix_anomaly_workspace_status",
        "anomaly_alerts",
        ["workspace_id", "status", sa.text("created_at DESC")],
    )

    op.execute("ALTER TABLE anomaly_alerts ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY anomaly_select ON anomaly_alerts FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute(
        "CREATE POLICY anomaly_service ON anomaly_alerts USING (auth.role() = 'service_role')"
    )

    # ------------------------------------------------------------------
    # mv_workspace_daily — one row per (workspace_id, day)
    # Rolls up:
    #   - public API invocations (api_request_log)
    #   - tool calls (tool_invocations)
    #   - vetting runs completed (vetting_runs)
    #   - audit events of interest (agent.test, document.upload)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE MATERIALIZED VIEW mv_workspace_daily AS
        WITH days AS (
            SELECT
                workspace_id,
                date_trunc('day', ts)::date AS day,
                count(*)::int AS invocations,
                avg(latency_ms)::int AS avg_latency_ms,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::int AS p95_latency_ms,
                count(*) FILTER (WHERE status_code >= 200 AND status_code < 300)::int AS invocation_success,
                count(DISTINCT api_key_id)::int AS unique_api_keys
            FROM api_request_log
            GROUP BY 1, 2
        ),
        tools AS (
            SELECT workspace_id, date_trunc('day', created_at)::date AS day,
                   count(*)::int AS tool_calls,
                   count(*) FILTER (WHERE outcome = 'success')::int AS tool_success
            FROM tool_invocations
            GROUP BY 1, 2
        ),
        vets AS (
            SELECT workspace_id, date_trunc('day', completed_at)::date AS day,
                   count(*)::int AS vetting_runs_completed
            FROM vetting_runs
            WHERE status = 'completed'
            GROUP BY 1, 2
        ),
        sandbox AS (
            SELECT actor_workspace_id AS workspace_id,
                   date_trunc('day', timestamp)::date AS day,
                   count(*)::int AS agent_tests
            FROM audit_log
            WHERE action = 'agent.test'
            GROUP BY 1, 2
        ),
        uploads AS (
            SELECT actor_workspace_id AS workspace_id,
                   date_trunc('day', timestamp)::date AS day,
                   count(*)::int AS documents_uploaded
            FROM audit_log
            WHERE action = 'document.upload'
            GROUP BY 1, 2
        )
        SELECT
            COALESCE(d.workspace_id, t.workspace_id, v.workspace_id, s.workspace_id, u.workspace_id) AS workspace_id,
            COALESCE(d.day, t.day, v.day, s.day, u.day) AS day,
            COALESCE(d.invocations, 0) AS invocations,
            COALESCE(d.avg_latency_ms, 0) AS avg_latency_ms,
            COALESCE(d.p95_latency_ms, 0) AS p95_latency_ms,
            COALESCE(d.invocation_success, 0) AS invocation_success,
            COALESCE(d.unique_api_keys, 0) AS unique_api_keys,
            COALESCE(t.tool_calls, 0) AS tool_calls,
            COALESCE(t.tool_success, 0) AS tool_success,
            COALESCE(v.vetting_runs_completed, 0) AS vetting_runs_completed,
            COALESCE(s.agent_tests, 0) AS agent_tests,
            COALESCE(u.documents_uploaded, 0) AS documents_uploaded
        FROM days d
        FULL OUTER JOIN tools t  ON d.workspace_id = t.workspace_id AND d.day = t.day
        FULL OUTER JOIN vets  v  ON COALESCE(d.workspace_id, t.workspace_id) = v.workspace_id AND COALESCE(d.day, t.day) = v.day
        FULL OUTER JOIN sandbox s ON COALESCE(d.workspace_id, t.workspace_id, v.workspace_id) = s.workspace_id AND COALESCE(d.day, t.day, v.day) = s.day
        FULL OUTER JOIN uploads u ON COALESCE(d.workspace_id, t.workspace_id, v.workspace_id, s.workspace_id) = u.workspace_id AND COALESCE(d.day, t.day, v.day, s.day) = u.day
        WITH NO DATA;
    """)
    # CONCURRENT refresh requires a unique index.
    op.create_index(
        "ux_mv_workspace_daily",
        "mv_workspace_daily",
        ["workspace_id", "day"],
        unique=True,
    )


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mv_workspace_daily")
    op.execute("DROP POLICY IF EXISTS anomaly_service ON anomaly_alerts")
    op.execute("DROP POLICY IF EXISTS anomaly_select ON anomaly_alerts")
    op.drop_table("anomaly_alerts")
