"""Phase 7 — Developer SDK & API

- api_request_log: sliding-window source for per-API-key rate limiting
- webhook_subscriptions: per-workspace event subscriptions with HMAC secrets
- webhook_deliveries: durable delivery log with retry state

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # api_request_log — every external (API-key-authenticated) request
    # ------------------------------------------------------------------
    op.create_table(
        "api_request_log",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "api_key_id",
            UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            nullable=False,
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
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("status_code", sa.SmallInteger, nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "ts",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Hot path: count requests for a key in the last N seconds.
    op.create_index(
        "ix_api_request_log_key_ts",
        "api_request_log",
        ["api_key_id", sa.text("ts DESC")],
    )
    op.create_index(
        "ix_api_request_log_workspace_ts",
        "api_request_log",
        ["workspace_id", sa.text("ts DESC")],
    )

    op.execute("ALTER TABLE api_request_log ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY api_request_log_select ON api_request_log FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute(
        "CREATE POLICY api_request_log_service ON api_request_log "
        "USING (auth.role() = 'service_role')"
    )

    # ------------------------------------------------------------------
    # webhook_subscriptions
    # ------------------------------------------------------------------
    op.create_table(
        "webhook_subscriptions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("events", sa.ARRAY(sa.Text), nullable=False),
        # The HMAC signing secret. Stored as plaintext (standard webhook
        # design — receivers need the same bytes we sign with, and Argon2id
        # would be one-way). Treat as a credential: leak protections via
        # RLS + access-control on this table. Encrypt-at-rest column with
        # an app-level key is a follow-on (see PHASE_LOG.md).
        sa.Column("signing_secret", sa.Text, nullable=False),
        sa.Column(
            "secret_prefix",
            sa.String(16),
            nullable=False,
            server_default="",
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column(
            "created_by",
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
        sa.CheckConstraint(
            "status IN ('active','paused','disabled')",
            name="webhook_subscription_status",
        ),
    )
    op.create_index(
        "ix_webhook_subscription_workspace",
        "webhook_subscriptions",
        ["workspace_id", "status"],
    )

    op.execute("ALTER TABLE webhook_subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY webhook_subscription_select ON webhook_subscriptions FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute(
        "CREATE POLICY webhook_subscription_service ON webhook_subscriptions "
        "USING (auth.role() = 'service_role')"
    )

    # ------------------------------------------------------------------
    # webhook_deliveries — durable retry queue + audit log
    # ------------------------------------------------------------------
    op.create_table(
        "webhook_deliveries",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "subscription_id",
            UUID(as_uuid=True),
            sa.ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event", sa.String(80), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("delivery_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("attempt_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.SmallInteger, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','success','failed','dead')",
            name="webhook_delivery_status",
        ),
    )
    # Worker scans this: pending deliveries due for retry.
    op.create_index(
        "ix_webhook_delivery_pending",
        "webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_webhook_delivery_subscription",
        "webhook_deliveries",
        ["subscription_id", sa.text("created_at DESC")],
    )

    op.execute("ALTER TABLE webhook_deliveries ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY webhook_delivery_select ON webhook_deliveries FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute(
        "CREATE POLICY webhook_delivery_service ON webhook_deliveries "
        "USING (auth.role() = 'service_role')"
    )


def downgrade() -> None:
    for table in ("webhook_deliveries", "webhook_subscriptions", "api_request_log"):
        op.execute(f"DROP POLICY IF EXISTS {table[:-1]}_service ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table[:-1]}_select ON {table}")
        op.drop_table(table)
