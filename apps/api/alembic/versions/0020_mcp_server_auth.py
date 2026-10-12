"""MCP server Tier-1 auth — static token/API key headers.

Adds optional, Fernet-encrypted auth to mcp_servers so authenticated MCP
servers (Hugging Face, and any bearer/API-key server) can be registered.
The token blob is never returned via API or logged. NULL = no auth.

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-30
"""

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("auth_value_encrypted", sa.LargeBinary(), nullable=True))
    op.add_column(
        "mcp_servers",
        sa.Column(
            "auth_header",
            sa.String(length=64),
            nullable=False,
            server_default="Authorization",
        ),
    )
    op.add_column(
        "mcp_servers",
        sa.Column("auth_scheme", sa.String(length=20), nullable=False, server_default="Bearer"),
    )


def downgrade() -> None:
    op.drop_column("mcp_servers", "auth_scheme")
    op.drop_column("mcp_servers", "auth_header")
    op.drop_column("mcp_servers", "auth_value_encrypted")
