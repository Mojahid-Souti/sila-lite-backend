"""Phase B multi-tenancy — n8n_user_links mapping table.

One row per Sila (Supabase) user, mapping to a private n8n user. The
n8n_password_encrypted column stores a Fernet-encrypted blob; it MUST
never be returned via API responses or logged.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-25
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "n8n_user_links",
        sa.Column(
            "supabase_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("auth.users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("n8n_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("n8n_email", sa.String(length=320), nullable=False),
        sa.Column("n8n_password_encrypted", sa.LargeBinary, nullable=False),
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
        sa.UniqueConstraint("n8n_user_id", name="uq_n8n_user_links_n8n_user_id"),
        sa.UniqueConstraint("n8n_email", name="uq_n8n_user_links_n8n_email"),
    )

    # No RLS — the table is read/written only by the API service role.
    # Disable explicitly so a future RLS audit confirms intent.
    op.execute("ALTER TABLE n8n_user_links DISABLE ROW LEVEL SECURITY;")


def downgrade() -> None:
    op.drop_table("n8n_user_links")
