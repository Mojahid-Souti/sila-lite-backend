"""Create agents table with RLS

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
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
            "name",
            sa.String(100),
            sa.CheckConstraint("char_length(name) BETWEEN 2 AND 100", name="agent_name_length"),
            nullable=False,
        ),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False),
        sa.Column("tags", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "status",
            sa.String(20),
            sa.CheckConstraint(
                "status IN ('draft','in_review','published','archived')",
                name="agent_valid_status",
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("language", sa.String(10), nullable=False, server_default="en"),
        sa.Column(
            "classification",
            sa.String(20),
            sa.CheckConstraint(
                "classification IN ('public','internal','restricted','confidential')",
                name="agent_valid_classification",
            ),
            nullable=False,
            server_default="internal",
        ),
        sa.Column("dify_app_id", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema="public",
    )

    # Composite index: supports list-agents query filtered by workspace+status, sorted by updated_at
    op.create_index(
        "ix_agents_workspace_status_updated",
        "agents",
        ["workspace_id", "status", sa.text("updated_at DESC")],
    )
    op.create_index("ix_agents_workspace_owner", "agents", ["workspace_id", "owner_id"])

    op.execute("ALTER TABLE agents ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY agent_select ON agents FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute("""
        CREATE POLICY agent_insert ON agents FOR INSERT
        WITH CHECK (workspace_id IN (
            SELECT workspace_id FROM memberships
            WHERE user_id = auth.uid() AND role IN ('builder','admin')
        ))
    """)
    op.execute("""
        CREATE POLICY agent_update ON agents FOR UPDATE
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships
            WHERE user_id = auth.uid() AND role IN ('builder','admin')
        ))
    """)
    op.execute("CREATE POLICY agent_service ON agents USING (auth.role() = 'service_role')")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS agent_service ON agents")
    op.execute("DROP POLICY IF EXISTS agent_update ON agents")
    op.execute("DROP POLICY IF EXISTS agent_insert ON agents")
    op.execute("DROP POLICY IF EXISTS agent_select ON agents")
    op.drop_index("ix_agents_workspace_owner", table_name="agents")
    op.drop_index("ix_agents_workspace_status_updated", table_name="agents")
    op.drop_table("agents")
