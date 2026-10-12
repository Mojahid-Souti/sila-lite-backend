"""AI-written title for a SILA session (shown in the Recents list).

Nullable; the concierge fills it after the first turn. The Recents list falls
back to the first user message when it's null.

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sila_sessions", sa.Column("title", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("sila_sessions", "title")
