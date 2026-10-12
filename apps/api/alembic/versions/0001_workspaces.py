"""Create workspaces table with RLS

Revision ID: 0001
Revises:
Create Date: 2026-05-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "workspaces",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column(
            "owner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("auth.users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("settings", JSONB, nullable=False, server_default="{}"),
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
        sa.CheckConstraint("char_length(name) BETWEEN 2 AND 100", name="workspace_name_length"),
        sa.CheckConstraint("slug ~ '^[a-z0-9-]+$'", name="workspace_slug_format"),
        schema="public",
    )
    op.create_index("ix_workspaces_slug", "workspaces", ["slug"], unique=True)
    op.create_index("ix_workspaces_owner_id", "workspaces", ["owner_id"])

    # RLS
    op.execute("ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY")
    # workspace_select and workspace_update reference memberships — added in 0002
    op.execute("""
        CREATE POLICY workspace_insert ON workspaces FOR INSERT
        WITH CHECK (owner_id = auth.uid())
    """)
    # Service role bypass (for API server using service key)
    op.execute("CREATE POLICY workspace_service ON workspaces USING (auth.role() = 'service_role')")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS workspace_service ON workspaces")
    op.execute("DROP POLICY IF EXISTS workspace_insert ON workspaces")
    # workspace_select and workspace_update are dropped in 0002 downgrade
    op.drop_index("ix_workspaces_owner_id", table_name="workspaces")
    op.drop_index("ix_workspaces_slug", table_name="workspaces")
    op.drop_table("workspaces")
