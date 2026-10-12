"""Phase 6 — Security Gatekeeper

- Adds `vetting_status` column to `agents` (tracks the safety-review state
  independently of the lifecycle `status` column).
- Creates `vetting_runs` (one row per submit-for-review) and
  `vetting_findings` (one row per detected issue within a run).
- RLS scoped to workspace members; only `service_role` may insert/update
  vetting rows so the audit history is tamper-resistant from the API path.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-21
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Agent: vetting_status column
    # ------------------------------------------------------------------
    op.add_column(
        "agents",
        sa.Column(
            "vetting_status",
            sa.String(30),
            nullable=False,
            server_default="unvetted",
        ),
    )
    op.create_check_constraint(
        "agent_valid_vetting_status",
        "agents",
        "vetting_status IN ('unvetted','scanning','ready_for_review','approved','rejected','failed')",
    )

    # ------------------------------------------------------------------
    # vetting_runs
    # ------------------------------------------------------------------
    op.create_table(
        "vetting_runs",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(30), nullable=False, server_default="scanning"),
        sa.Column("outcome", sa.String(30), nullable=True),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column(
            "triggered_by",
            UUID(as_uuid=True),
            sa.ForeignKey("auth.users.id"),
            nullable=False,
        ),
        sa.Column(
            "approved_by",
            UUID(as_uuid=True),
            sa.ForeignKey("auth.users.id"),
            nullable=True,
        ),
        sa.Column("approval_decision", sa.String(20), nullable=True),
        sa.Column("approval_note", sa.Text, nullable=True),
        sa.Column("finding_summary", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('scanning','completed','failed')", name="vetting_run_valid_status"
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('ready_for_review','auto_approved','rejected','error')",
            name="vetting_run_valid_outcome",
        ),
        sa.CheckConstraint(
            "approval_decision IS NULL OR approval_decision IN ('approved','rejected')",
            name="vetting_run_valid_approval",
        ),
    )
    op.create_index(
        "ix_vetting_run_agent_time",
        "vetting_runs",
        ["agent_id", sa.text("started_at DESC")],
    )
    op.create_index(
        "ix_vetting_run_workspace_time",
        "vetting_runs",
        ["workspace_id", sa.text("started_at DESC")],
    )

    op.execute("ALTER TABLE vetting_runs ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY vetting_run_select ON vetting_runs FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute(
        "CREATE POLICY vetting_run_service ON vetting_runs USING (auth.role() = 'service_role')"
    )

    # ------------------------------------------------------------------
    # vetting_findings
    # ------------------------------------------------------------------
    op.create_table(
        "vetting_findings",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "vetting_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("vetting_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("finding_type", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("location", sa.String(50), nullable=False),
        sa.Column("matched_preview", sa.Text, nullable=False, server_default=""),
        sa.Column("matched_full", sa.Text, nullable=True),
        sa.Column("finding_metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "severity IN ('info','low','medium','high','critical')",
            name="vetting_finding_valid_severity",
        ),
    )
    op.create_index("ix_vetting_finding_run", "vetting_findings", ["vetting_run_id", "severity"])
    op.create_index("ix_vetting_finding_workspace", "vetting_findings", ["workspace_id"])

    op.execute("ALTER TABLE vetting_findings ENABLE ROW LEVEL SECURITY")
    # Non-admin members see preview only; admin sees both (column-level enforcement
    # at the application layer — RLS allows row access here, app layer hides matched_full).
    op.execute("""
        CREATE POLICY vetting_finding_select ON vetting_findings FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute(
        "CREATE POLICY vetting_finding_service ON vetting_findings USING (auth.role() = 'service_role')"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS vetting_finding_service ON vetting_findings")
    op.execute("DROP POLICY IF EXISTS vetting_finding_select ON vetting_findings")
    op.drop_table("vetting_findings")
    op.execute("DROP POLICY IF EXISTS vetting_run_service ON vetting_runs")
    op.execute("DROP POLICY IF EXISTS vetting_run_select ON vetting_runs")
    op.drop_table("vetting_runs")
    op.drop_constraint("agent_valid_vetting_status", "agents", type_="check")
    op.drop_column("agents", "vetting_status")
