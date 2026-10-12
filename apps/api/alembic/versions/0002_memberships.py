"""Create memberships table with RLS

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memberships",
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
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("auth.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("invited_by", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "role IN ('admin','builder','reviewer','hirer','viewer')", name="valid_role"
        ),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_membership"),
        schema="public",
    )
    # Unique index doubles as the O(1) permission lookup index
    op.create_index(
        "ix_memberships_workspace_user", "memberships", ["workspace_id", "user_id"], unique=True
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])

    op.execute("ALTER TABLE memberships ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY membership_select ON memberships FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute(
        "CREATE POLICY membership_service ON memberships USING (auth.role() = 'service_role')"
    )

    # Workspace RLS policies that reference memberships — deferred from 0001
    op.execute("""
        CREATE POLICY workspace_select ON workspaces FOR SELECT
        USING (id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute("""
        CREATE POLICY workspace_update ON workspaces FOR UPDATE
        USING (id IN (
            SELECT workspace_id FROM memberships
            WHERE user_id = auth.uid() AND role = 'admin'
        ))
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS workspace_update ON workspaces")
    op.execute("DROP POLICY IF EXISTS workspace_select ON workspaces")
    op.execute("DROP POLICY IF EXISTS membership_service ON memberships")
    op.execute("DROP POLICY IF EXISTS membership_select ON memberships")
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_index("ix_memberships_workspace_user", table_name="memberships")
    op.drop_table("memberships")
