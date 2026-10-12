"""Agent feedback — likes, dislikes, reports.

Phase 15: Bazaar feedback. Denormalized counters on `agents`
(liked_count / disliked_count / report_count) give O(1) reads for the trending
card and cards, while two audit tables keep the source of truth for the future
admin system:

  - agent_reactions: one like|dislike per user per agent (UNIQUE), toggleable.
  - agent_reports:   user-submitted reports with a review status, RLS-scoped.

Both tables are workspace-scoped via RLS (members see their workspace; the
service role bypasses for server-side counter maintenance).

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-10
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- denormalized counters on agents (O(1) trending sort / card display) ---
    for col in ("liked_count", "disliked_count", "report_count"):
        op.add_column(
            "agents",
            sa.Column(col, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )
    # Top-liked Published agents — backs GET /bazaar/trending.
    op.create_index(
        "ix_agents_trending",
        "agents",
        ["status", sa.text("liked_count DESC")],
    )

    # --- agent_reactions: one reaction per (agent, user) ---
    op.create_table(
        "agent_reactions",
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
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False),
        sa.Column("reaction", sa.String(length=10), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("reaction IN ('like','dislike')", name="agent_reaction_valid"),
        sa.UniqueConstraint("agent_id", "user_id", name="uq_agent_reaction"),
    )
    op.create_index("ix_agent_reactions_agent", "agent_reactions", ["agent_id"])

    op.execute("ALTER TABLE agent_reactions ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY agent_reactions_select ON agent_reactions FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute("""
        CREATE POLICY agent_reactions_service ON agent_reactions
        USING (auth.role() = 'service_role')
    """)

    # --- agent_reports: user reports, reviewed later in the admin system ---
    op.create_table(
        "agent_reports",
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
        sa.Column(
            "reporter_id", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_by", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=True),
        sa.CheckConstraint("char_length(reason) <= 2000", name="agent_report_reason_max"),
        sa.CheckConstraint(
            "status IN ('open','reviewing','resolved','dismissed')",
            name="agent_report_valid_status",
        ),
    )
    op.create_index(
        "ix_agent_reports_queue",
        "agent_reports",
        ["workspace_id", "status", sa.text("created_at DESC")],
    )
    op.create_index("ix_agent_reports_agent", "agent_reports", ["agent_id"])

    op.execute("ALTER TABLE agent_reports ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY agent_reports_select ON agent_reports FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute("""
        CREATE POLICY agent_reports_service ON agent_reports
        USING (auth.role() = 'service_role')
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS agent_reports_service ON agent_reports")
    op.execute("DROP POLICY IF EXISTS agent_reports_select ON agent_reports")
    op.drop_index("ix_agent_reports_agent", table_name="agent_reports")
    op.drop_index("ix_agent_reports_queue", table_name="agent_reports")
    op.drop_table("agent_reports")

    op.execute("DROP POLICY IF EXISTS agent_reactions_service ON agent_reactions")
    op.execute("DROP POLICY IF EXISTS agent_reactions_select ON agent_reactions")
    op.drop_index("ix_agent_reactions_agent", table_name="agent_reactions")
    op.drop_table("agent_reactions")

    op.drop_index("ix_agents_trending", table_name="agents")
    for col in ("report_count", "disliked_count", "liked_count"):
        op.drop_column("agents", col)
