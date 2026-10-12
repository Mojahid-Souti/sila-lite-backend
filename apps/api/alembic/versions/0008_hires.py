"""Create hires table with RLS

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-18
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hires",
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
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("hired_by", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False),
        sa.Column(
            "hired_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint("workspace_id", "agent_id", name="uq_hire"),
    )

    op.create_index(
        "ix_hires_workspace_hired_at", "hires", ["workspace_id", sa.text("hired_at DESC")]
    )

    op.execute("ALTER TABLE hires ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY hires_select ON hires FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute("""
        CREATE POLICY hires_service ON hires
        USING (auth.role() = 'service_role')
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS hires_service ON hires")
    op.execute("DROP POLICY IF EXISTS hires_select ON hires")
    op.drop_index("ix_hires_workspace_hired_at", table_name="hires")
    op.drop_table("hires")
