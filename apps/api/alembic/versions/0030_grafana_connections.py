"""Grafana connections — the first self-hosted-URL connector.

One row per workspace: the company's Grafana base URL + a Fernet-encrypted API
token. Unlike the OAuth connectors (which go through Nango and store only a
connection reference), Grafana lives on the user's OWN instance, so we store the
URL + token ourselves (token encrypted at rest, never returned to the client).

RLS is per-workspace (a workspace-shared resource, admin-managed — like
mcp_servers): any member of the workspace can read the config's existence, but
the token column is never selected back to clients. The API service_role
bypasses for execution.

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-06
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grafana_connections",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("base_url", sa.Text(), nullable=False),
        # Fernet-encrypted API token (LargeBinary). Never returned to the client.
        sa.Column("token_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "registered_by", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
        ),
        # One Grafana per workspace.
        sa.UniqueConstraint("workspace_id", name="uq_grafana_connection_workspace"),
    )

    op.execute("ALTER TABLE grafana_connections ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY grafana_connections_select ON grafana_connections FOR SELECT
        USING (
            workspace_id IN (SELECT workspace_id FROM memberships WHERE user_id = auth.uid())
        )
    """)
    op.execute("""
        CREATE POLICY grafana_connections_service ON grafana_connections
        USING (auth.role() = 'service_role')
    """)


def downgrade() -> None:
    for policy in ("grafana_connections_service", "grafana_connections_select"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON grafana_connections")
    op.drop_table("grafana_connections")
