"""Connector bindings — Nango OAuth authorizations (WS-9).

One row per (user, workspace, provider) recording that a user authorized a SaaS
app through self-hosted Nango. Stores only the opaque `nango_connection_id`
reference + the user's resource choices — NEVER a token (tokens live encrypted
inside Nango under our NANGO_ENCRYPTION_KEY).

RLS is **per-user** (not just per-workspace): a user can only read/write/use
their own connections — the floor for "a user can only use their own
nango_connection_id". The API service role bypasses RLS as usual.

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-25
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_bindings",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("auth.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("nango_connection_id", sa.String(length=255), nullable=False),
        sa.Column(
            "selected_resources", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "user_id", "workspace_id", "provider", name="uq_connector_binding_user_ws_provider"
        ),
    )
    # Hot path: "does this user have a binding for this provider in this ws?" O(log n).
    op.create_index(
        "ix_connector_bindings_user_provider",
        "connector_bindings",
        ["user_id", "workspace_id", "provider"],
    )

    # RLS — per-user. A member can only touch their OWN bindings, and only within
    # a workspace they belong to. The API (service_role) bypasses for execution.
    op.execute("ALTER TABLE connector_bindings ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY connector_bindings_select ON connector_bindings FOR SELECT
        USING (
            user_id = auth.uid()
            AND workspace_id IN (SELECT workspace_id FROM memberships WHERE user_id = auth.uid())
        )
    """)
    op.execute("""
        CREATE POLICY connector_bindings_insert ON connector_bindings FOR INSERT
        WITH CHECK (
            user_id = auth.uid()
            AND workspace_id IN (SELECT workspace_id FROM memberships WHERE user_id = auth.uid())
        )
    """)
    op.execute("""
        CREATE POLICY connector_bindings_update ON connector_bindings FOR UPDATE
        USING (user_id = auth.uid())
    """)
    op.execute("""
        CREATE POLICY connector_bindings_delete ON connector_bindings FOR DELETE
        USING (user_id = auth.uid())
    """)
    op.execute("""
        CREATE POLICY connector_bindings_service ON connector_bindings
        USING (auth.role() = 'service_role')
    """)


def downgrade() -> None:
    for policy in (
        "connector_bindings_service",
        "connector_bindings_delete",
        "connector_bindings_update",
        "connector_bindings_insert",
        "connector_bindings_select",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON connector_bindings")
    op.drop_index("ix_connector_bindings_user_provider", table_name="connector_bindings")
    op.drop_table("connector_bindings")
