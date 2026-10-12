"""SILA per-user memory — durable facts SILA learns about a user.

Mojahid's design: per USER (not per chat, not per workspace — it's "about
you": role, preferences, corrections like "always use medians"). Capacity-
capped; when full the composer blocks sending until the user deletes entries
(the UI manages it under Account & Usage → Memory, clear-all behind a
confirm). SILA writes via the `remember` tool; every session's system prompt
carries the current entries.

RLS: per-user — a user can only see/delete their own memories. The API also
scopes every query by user_id (defense in depth).

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-10
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sila_user_memories",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False),
        sa.Column("content", sa.String(400), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_sila_user_memories_user", "sila_user_memories", ["user_id"])
    op.execute("ALTER TABLE sila_user_memories ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY sila_user_memories_owner ON sila_user_memories
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid())
    """)


def downgrade() -> None:
    op.drop_index("ix_sila_user_memories_user", table_name="sila_user_memories")
    op.drop_table("sila_user_memories")
