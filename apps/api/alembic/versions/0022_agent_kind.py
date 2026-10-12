"""Agent kind — chat (Dify-backed) vs workflow (n8n-webhook-backed).

Phase 15 Surface 3: workflow agents. Adds `kind` (CHECK chat|workflow,
default chat so every existing agent stays a chat agent) and
`n8n_workflow_id` (set only for kind='workflow').

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-10
"""

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="chat"),
    )
    op.add_column("agents", sa.Column("n8n_workflow_id", sa.Text(), nullable=True))
    op.create_check_constraint("agent_valid_kind", "agents", "kind IN ('chat','workflow')")


def downgrade() -> None:
    op.drop_constraint("agent_valid_kind", "agents", type_="check")
    op.drop_column("agents", "n8n_workflow_id")
    op.drop_column("agents", "kind")
