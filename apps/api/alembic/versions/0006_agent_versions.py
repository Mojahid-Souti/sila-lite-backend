"""Create agent_versions table with RLS

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_versions",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_num", sa.Integer, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("dify_dsl", JSONB, nullable=False),
        sa.Column("changed_by", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False),
        sa.Column("change_note", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("agent_id", "version_num", name="uq_agent_version"),
        schema="public",
    )

    # Unique constraint creates an implicit index; add explicit DESC for point lookups
    op.create_index(
        "ix_agent_versions_agent_version",
        "agent_versions",
        ["agent_id", sa.text("version_num DESC")],
    )

    op.execute("ALTER TABLE agent_versions ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY agent_version_select ON agent_versions FOR SELECT
        USING (agent_id IN (
            SELECT id FROM agents WHERE workspace_id IN (
                SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
            )
        ))
    """)
    op.execute(
        "CREATE POLICY agent_version_service ON agent_versions USING (auth.role() = 'service_role')"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS agent_version_service ON agent_versions")
    op.execute("DROP POLICY IF EXISTS agent_version_select ON agent_versions")
    op.drop_index("ix_agent_versions_agent_version", table_name="agent_versions")
    op.drop_table("agent_versions")
