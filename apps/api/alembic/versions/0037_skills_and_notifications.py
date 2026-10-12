"""Skills in the Bazaar, and per-user notifications (Phase 18).

`skills` — open-source Agent Skills (a SKILL.md: a name, a description and
markdown instructions) imported by a workspace Admin from an upstream catalog,
scanned by the gatekeeper's text scanners, and approved by an Admin/Reviewer
before they appear in the platform-wide Bazaar. Only the instructions are
stored; a skill's scripts are never downloaded or run. One row per upstream
skill (source + skill key), owned by the importing workspace.

`notifications` — the header bell: a row per event a user should see (an agent
hired, a template or skill picked, an import vetted). Owner-only.

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("skill_key", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("repo_url", sa.String(length=300), nullable=False),
        sa.Column("license", sa.String(length=120), nullable=True),
        sa.Column("installs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("has_scripts", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dependencies", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_sha256", sa.String(length=64), nullable=False),
        sa.Column("vetting_status", sa.String(length=20), nullable=False),
        sa.Column("findings", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "imported_by", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False
        ),
        sa.Column("imported_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("reviewed_by", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("source", "skill_key", name="uq_skill_upstream"),
        sa.CheckConstraint(
            "vetting_status IN ('needs_review','approved','rejected')", name="skill_vetting_status"
        ),
        # Published implies approved: nothing unvetted reaches the Bazaar.
        sa.CheckConstraint(
            "NOT published OR vetting_status = 'approved'", name="skill_published_is_approved"
        ),
    )
    # Full-text search over what a reader sees on the card.
    op.execute("""
        ALTER TABLE skills ADD COLUMN search tsvector
        GENERATED ALWAYS AS (to_tsvector('english', name || ' ' || description)) STORED
    """)
    op.create_index("ix_skills_search", "skills", ["search"], postgresql_using="gin")
    op.execute("CREATE INDEX ix_skills_listing ON skills (published, category, installs DESC)")
    op.create_index("ix_skills_workspace", "skills", ["workspace_id"])

    op.execute("ALTER TABLE skills ENABLE ROW LEVEL SECURITY")
    # Published skills are the platform-wide Bazaar; the importing workspace also
    # sees its own drafts and rejections. Only the API (service_role) writes.
    op.execute("""
        CREATE POLICY skills_select ON skills FOR SELECT
        USING (
            published
            OR workspace_id IN (SELECT workspace_id FROM memberships WHERE user_id = auth.uid())
        )
    """)
    op.execute("""
        CREATE POLICY skills_service ON skills
        USING (auth.role() = 'service_role')
    """)

    op.create_table(
        "notifications",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("link", sa.String(length=300), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.execute(
        "CREATE INDEX ix_notifications_user_created ON notifications (user_id, created_at DESC)"
    )
    # The bell's unread badge: only unread rows, so the count stays O(log n).
    op.execute(
        "CREATE INDEX ix_notifications_unread ON notifications (user_id) WHERE read_at IS NULL"
    )
    op.execute("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY notifications_owner ON notifications
        USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid())
    """)
    op.execute("""
        CREATE POLICY notifications_service ON notifications
        USING (auth.role() = 'service_role')
    """)


def downgrade() -> None:
    for policy in ("notifications_service", "notifications_owner"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON notifications")
    op.drop_table("notifications")
    for policy in ("skills_service", "skills_select"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON skills")
    op.drop_table("skills")
