"""Phase 5 — Tools, MCP & integrations

Creates mcp_servers, tools, agent_tools, tool_invocations.
RLS policies scope all reads to workspace members; writes go through service_role.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-21
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # mcp_servers
    # ------------------------------------------------------------------
    op.create_table(
        "mcp_servers",
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
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("transport", sa.String(20), nullable=False, server_default="http"),
        sa.Column("is_builtin", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column(
            "registered_by",
            UUID(as_uuid=True),
            sa.ForeignKey("auth.users.id"),
            nullable=False,
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
        sa.UniqueConstraint("workspace_id", "name", name="uq_mcp_server_workspace_name"),
        sa.CheckConstraint("char_length(name) BETWEEN 2 AND 100", name="mcp_server_name_length"),
        sa.CheckConstraint("transport IN ('http')", name="mcp_server_transport"),
        sa.CheckConstraint("status IN ('active','disabled')", name="mcp_server_status"),
    )
    op.create_index("ix_mcp_server_workspace", "mcp_servers", ["workspace_id", "status"])

    op.execute("ALTER TABLE mcp_servers ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY mcp_server_select ON mcp_servers FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute(
        "CREATE POLICY mcp_server_service ON mcp_servers USING (auth.role() = 'service_role')"
    )

    # ------------------------------------------------------------------
    # tools — populated by the discovery step
    # ------------------------------------------------------------------
    op.create_table(
        "tools",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "mcp_server_id",
            UUID(as_uuid=True),
            sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("input_schema", JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
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
        sa.UniqueConstraint("mcp_server_id", "name", name="uq_tool_server_name"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="tool_name_length"),
        sa.CheckConstraint("status IN ('active','disabled')", name="tool_status"),
    )
    op.create_index("ix_tool_workspace", "tools", ["workspace_id", "status"])
    op.create_index("ix_tool_server", "tools", ["mcp_server_id"])

    op.execute("ALTER TABLE tools ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tool_select ON tools FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute("CREATE POLICY tool_service ON tools USING (auth.role() = 'service_role')")

    # ------------------------------------------------------------------
    # agent_tools — composite PK (agent_id, tool_id)
    # ------------------------------------------------------------------
    op.create_table(
        "agent_tools",
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tool_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tools.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "granted_by",
            UUID(as_uuid=True),
            sa.ForeignKey("auth.users.id"),
            nullable=False,
        ),
        sa.Column(
            "granted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_agent_tool_tool", "agent_tools", ["tool_id"])
    op.create_index("ix_agent_tool_workspace", "agent_tools", ["workspace_id"])

    op.execute("ALTER TABLE agent_tools ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY agent_tool_select ON agent_tools FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute(
        "CREATE POLICY agent_tool_service ON agent_tools USING (auth.role() = 'service_role')"
    )

    # ------------------------------------------------------------------
    # tool_invocations
    # ------------------------------------------------------------------
    op.create_table(
        "tool_invocations",
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
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "tool_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tools.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "caller_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("auth.users.id"),
            nullable=True,
        ),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("input_preview", sa.Text, nullable=False, server_default=""),
        sa.Column("output_hash", sa.String(64), nullable=True),
        sa.Column("output_preview", sa.Text, nullable=False, server_default=""),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "outcome IN ('success','failure','timeout')", name="tool_invocation_outcome"
        ),
    )
    op.create_index(
        "ix_tool_invocation_workspace_time",
        "tool_invocations",
        ["workspace_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_tool_invocation_agent_time",
        "tool_invocations",
        ["agent_id", sa.text("created_at DESC")],
    )

    op.execute("ALTER TABLE tool_invocations ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tool_invocation_select ON tool_invocations FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute(
        "CREATE POLICY tool_invocation_service ON tool_invocations USING (auth.role() = 'service_role')"
    )


def downgrade() -> None:
    for table in ("tool_invocations", "agent_tools", "tools", "mcp_servers"):
        op.execute(f"DROP POLICY IF EXISTS {table[:-1]}_service ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table[:-1]}_select ON {table}")
        op.drop_table(table)
