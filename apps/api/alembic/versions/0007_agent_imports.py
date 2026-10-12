"""Create agent_imports table with RLS

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_imports",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "imported_by", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False
        ),
        sa.Column(
            "source",
            sa.String(20),
            sa.CheckConstraint(
                "source IN ('yaml_upload','template')", name="agent_import_valid_source"
            ),
            nullable=False,
        ),
        sa.Column("filename", sa.Text, nullable=True),
        # Truncated at 10_000 chars; full DSL lives in agent_versions
        sa.Column("raw_yaml", sa.Text, nullable=True),
        sa.Column(
            "status",
            sa.String(10),
            sa.CheckConstraint("status IN ('success','failed')", name="agent_import_valid_status"),
            nullable=False,
        ),
        sa.Column("error_msg", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema="public",
    )

    op.create_index(
        "ix_agent_imports_workspace_created",
        "agent_imports",
        ["workspace_id", sa.text("created_at DESC")],
    )

    op.execute("ALTER TABLE agent_imports ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY agent_import_select ON agent_imports FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute(
        "CREATE POLICY agent_import_service ON agent_imports USING (auth.role() = 'service_role')"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS agent_import_service ON agent_imports")
    op.execute("DROP POLICY IF EXISTS agent_import_select ON agent_imports")
    op.drop_index("ix_agent_imports_workspace_created", table_name="agent_imports")
    op.drop_table("agent_imports")
