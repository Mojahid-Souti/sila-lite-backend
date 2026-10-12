"""Create reviews table with RLS

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-18
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reviews",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("author_id", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False),
        sa.Column("rating", sa.SmallInteger, nullable=False),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("agent_id", "author_id", name="uq_review"),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="review_valid_rating"),
        sa.CheckConstraint("char_length(body) <= 2000", name="review_body_max"),
    )

    op.create_index(
        "ix_reviews_agent_created_at", "reviews", ["agent_id", sa.text("created_at DESC")]
    )

    op.execute("ALTER TABLE reviews ENABLE ROW LEVEL SECURITY")
    # Reviews are visible to any authenticated user (public ratings)
    op.execute("""
        CREATE POLICY reviews_select ON reviews FOR SELECT
        USING (auth.role() = 'authenticated' OR auth.role() = 'service_role')
    """)
    op.execute("""
        CREATE POLICY reviews_service ON reviews
        USING (auth.role() = 'service_role')
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS reviews_service ON reviews")
    op.execute("DROP POLICY IF EXISTS reviews_select ON reviews")
    op.drop_index("ix_reviews_agent_created_at", table_name="reviews")
    op.drop_table("reviews")
