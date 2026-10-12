"""Per-turn trace for a SILA assistant message (rehydrate the live timeline).

Nullable JSONB holding the collapsed process-trace (interleaved text + tool steps)
for an assistant turn that had tool activity. The chat replays it on reopen so the
vetting/tool steps come back, not just the text. Simple turns leave it null.

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sila_messages", sa.Column("trace", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("sila_messages", "trace")
