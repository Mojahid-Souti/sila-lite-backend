"""Create audit_log table (ECS-compatible) with RLS

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "timestamp",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "actor_user_id", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=True
        ),
        sa.Column(
            "actor_workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=True
        ),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("resource_type", sa.Text, nullable=True),
        sa.Column("resource_id", UUID(as_uuid=True), nullable=True),
        sa.Column("outcome", sa.String(10), nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.CheckConstraint("outcome IN ('success','failure')", name="valid_outcome"),
        schema="public",
    )
    # Workspace-scoped time-series queries for the Command Center (Phase 8)
    op.create_index(
        "ix_audit_workspace_time", "audit_log", ["actor_workspace_id", sa.text("timestamp DESC")]
    )
    op.create_index("ix_audit_user_time", "audit_log", ["actor_user_id", sa.text("timestamp DESC")])

    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY audit_select ON audit_log FOR SELECT
        USING (actor_workspace_id IN (
            SELECT workspace_id FROM memberships
            WHERE user_id = auth.uid() AND role = 'admin'
        ))
    """)
    # Only service_role can write audit entries (API server uses service key)
    op.execute("CREATE POLICY audit_service ON audit_log USING (auth.role() = 'service_role')")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS audit_service ON audit_log")
    op.execute("DROP POLICY IF EXISTS audit_select ON audit_log")
    op.drop_index("ix_audit_user_time", table_name="audit_log")
    op.drop_index("ix_audit_workspace_time", table_name="audit_log")
    op.drop_table("audit_log")
