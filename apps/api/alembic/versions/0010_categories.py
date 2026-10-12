"""Create categories and agent_categories tables with RLS + seed data

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-18
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_SEED_CATEGORIES = [
    ("Customer Support", "customer-support"),
    ("HR", "hr"),
    ("Finance", "finance"),
    ("Legal", "legal"),
    ("IT Ops", "it-ops"),
    ("Analytics", "analytics"),
]


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("slug", sa.String(60), nullable=False, unique=True),
        sa.CheckConstraint("char_length(name) BETWEEN 2 AND 50", name="category_name_length"),
        sa.CheckConstraint("slug ~ '^[a-z0-9-]+$'", name="category_slug_format"),
    )

    op.create_table(
        "agent_categories",
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "category_id",
            UUID(as_uuid=True),
            sa.ForeignKey("categories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # Seed built-in categories
    categories_table = sa.table(
        "categories",
        sa.column("name", sa.Text),
        sa.column("slug", sa.String),
    )
    op.bulk_insert(categories_table, [{"name": n, "slug": s} for n, s in _SEED_CATEGORIES])

    op.execute("ALTER TABLE categories ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY categories_select ON categories FOR SELECT
        USING (auth.role() IN ('authenticated', 'service_role', 'anon'))
    """)
    op.execute("""
        CREATE POLICY categories_service ON categories
        USING (auth.role() = 'service_role')
    """)

    op.execute("ALTER TABLE agent_categories ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY agent_categories_select ON agent_categories FOR SELECT
        USING (auth.role() IN ('authenticated', 'service_role', 'anon'))
    """)
    op.execute("""
        CREATE POLICY agent_categories_service ON agent_categories
        USING (auth.role() = 'service_role')
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS agent_categories_service ON agent_categories")
    op.execute("DROP POLICY IF EXISTS agent_categories_select ON agent_categories")
    op.execute("DROP POLICY IF EXISTS categories_service ON categories")
    op.execute("DROP POLICY IF EXISTS categories_select ON categories")
    op.drop_table("agent_categories")
    op.drop_table("categories")
