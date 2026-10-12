"""SILA concierge conversation state.

SILA Voice phase: `sila_sessions` (one row per WebSocket conversation) and
`sila_messages` (transcript: user / sila / tool turns). No raw audio is ever
stored — transcripts only (security review). Both tables RLS-scoped per
workspace, same policy pattern as 0023.

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-12
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sila_sessions",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    # Daily rate-limit lookup: sessions per user per day. O(log n).
    op.create_index(
        "ix_sila_sessions_user_started",
        "sila_sessions",
        ["user_id", sa.text("started_at DESC")],
    )

    op.create_table(
        "sila_messages",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sila_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("role IN ('user','sila','tool')", name="sila_message_valid_role"),
    )
    op.create_index("ix_sila_messages_session", "sila_messages", ["session_id", "created_at"])

    for table in ("sila_sessions", "sila_messages"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_select ON {table} FOR SELECT
            USING (workspace_id IN (
                SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
            ))
        """)
        op.execute(f"""
            CREATE POLICY {table}_service ON {table}
            USING (auth.role() = 'service_role')
        """)


def downgrade() -> None:
    for table in ("sila_messages", "sila_sessions"):
        op.execute(f"DROP POLICY IF EXISTS {table}_service ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_select ON {table}")
    op.drop_index("ix_sila_messages_session", table_name="sila_messages")
    op.drop_table("sila_messages")
    op.drop_index("ix_sila_sessions_user_started", table_name="sila_sessions")
    op.drop_table("sila_sessions")
