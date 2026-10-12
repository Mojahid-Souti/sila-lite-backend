"""Slide templates — corporate .pptx files SILA fills with generated content.

The Omantel pain point: staff HAVE the corporate template and the data but no
time to assemble decks. An admin uploads the .pptx once per workspace; SILA
generates slides INTO it (placeholder slides inherit the template's masters/
layouts/theme — logos, fonts, colors come free).

Bytes live in-row (bytea): templates are a few MB and a handful per workspace
— no object-store round-trip needed on the hot path. Size capped at the API.

RLS per-workspace (member-readable; writes are admin-gated at the API layer;
service_role bypasses for execution).

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-06
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pptx_templates",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("file_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column(
            "uploaded_by", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("workspace_id", "name", name="uq_pptx_template_workspace_name"),
    )
    op.create_index("ix_pptx_templates_workspace", "pptx_templates", ["workspace_id"])

    op.execute("ALTER TABLE pptx_templates ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY pptx_templates_select ON pptx_templates FOR SELECT
        USING (
            workspace_id IN (SELECT workspace_id FROM memberships WHERE user_id = auth.uid())
        )
    """)
    op.execute("""
        CREATE POLICY pptx_templates_service ON pptx_templates
        USING (auth.role() = 'service_role')
    """)


def downgrade() -> None:
    for policy in ("pptx_templates_service", "pptx_templates_select"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON pptx_templates")
    op.drop_index("ix_pptx_templates_workspace", table_name="pptx_templates")
    op.drop_table("pptx_templates")
