"""Analysis Engine P0 — uploaded datasets (full files, DuckDB-profiled).

Chat attachments ride as message text and are capped (~100 KB) to protect the
model's context; that made big-file analysis WRONG (head-truncated). Large
data files now upload here instead: bytes on the API's disk, this row as the
handle, DuckDB reading the FULL file for every aggregate. Per-user RLS —
datasets are the caller's own working data.

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-11
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sila_datasets",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("path", sa.String(400), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_sila_datasets_user", "sila_datasets", ["user_id"])
    op.execute("ALTER TABLE sila_datasets ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY sila_datasets_owner ON sila_datasets
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid())
    """)


def downgrade() -> None:
    op.drop_index("ix_sila_datasets_user", table_name="sila_datasets")
    op.drop_table("sila_datasets")
