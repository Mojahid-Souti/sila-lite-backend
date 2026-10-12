"""Generated files — short-lived downloads for artifacts SILA builds in chat.

A slide deck needs a destination; when the user hasn't connected OneDrive the
deck must still reach them — as a plain download link in the chat. Rows are
capability URLs (unguessable UUIDv4 id, ~122 bits) with a 24h TTL, pruned on
insert. RLS: service-role only (the download endpoint is a capability URL —
possession of the id IS the authorization, same class as Grafana share links).

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-06
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generated_files",
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
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("content_type", sa.String(150), nullable=False),
        sa.Column("file_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_generated_files_created", "generated_files", ["created_at"])
    op.execute("ALTER TABLE generated_files ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY generated_files_service ON generated_files
        USING (auth.role() = 'service_role')
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS generated_files_service ON generated_files")
    op.drop_index("ix_generated_files_created", table_name="generated_files")
    op.drop_table("generated_files")
