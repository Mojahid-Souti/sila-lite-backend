"""Widen vetting_findings.location 50 → 255.

A finding's `location` is a node/parameter path (e.g.
"Slack (chat.postMessage).parameters.headerParameters[0].value") which routinely
exceeds 50 chars. The overflow raised StringDataRightTruncationError mid-INSERT and,
because the build + vetting share one request transaction (get_db), rolled back the
whole build — the agent and the vetting run were both lost. 255 comfortably fits node
paths; the repo also truncates defensively.

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-27
"""

import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "vetting_findings",
        "location",
        existing_type=sa.String(length=50),
        type_=sa.String(length=255),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "vetting_findings",
        "location",
        existing_type=sa.String(length=255),
        type_=sa.String(length=50),
        existing_nullable=False,
    )
