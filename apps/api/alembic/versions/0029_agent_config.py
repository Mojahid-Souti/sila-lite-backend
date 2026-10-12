"""Add agents.config (editable publisher config for built workflow agents).

The workspace card + the safe-fields edit path read/write this JSONB: schedule,
destinations (Slack channel / email recipient), kb_id, docs. It's the source of
truth — the n8n workflow definition is rebuilt FROM it on edit. Nullable (chat
agents + pre-existing rows have none).

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-29
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("config", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "config")
