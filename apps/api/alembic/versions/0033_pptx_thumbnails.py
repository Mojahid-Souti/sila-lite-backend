"""Slide-template thumbnails — rendered previews for the gallery.

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-07
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pptx_templates",
        sa.Column("thumbnails", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("pptx_templates", "thumbnails")
