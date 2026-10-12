"""Create knowledge_bases and kb_documents tables with RLS

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-20
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pgvector extension (needed for 0012 chunks; idempotent)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_bases",
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
        sa.Column("scope", sa.String(20), nullable=False, server_default="workspace"),
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=True,
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
        sa.CheckConstraint("char_length(name) BETWEEN 2 AND 100", name="kb_name_length"),
        sa.CheckConstraint("scope IN ('workspace','agent')", name="kb_valid_scope"),
        sa.CheckConstraint("status IN ('active','archived')", name="kb_valid_status"),
        schema="public",
    )
    op.create_index("ix_kbs_workspace_id", "knowledge_bases", ["workspace_id"])
    op.create_index(
        "ix_kbs_agent_id",
        "knowledge_bases",
        ["agent_id"],
        postgresql_where=sa.text("agent_id IS NOT NULL"),
    )

    op.execute("ALTER TABLE knowledge_bases ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY kb_select ON knowledge_bases FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute("""
        CREATE POLICY kb_insert ON knowledge_bases FOR INSERT
        WITH CHECK (workspace_id IN (
            SELECT workspace_id FROM memberships
            WHERE user_id = auth.uid() AND role IN ('admin','builder')
        ))
    """)
    op.execute("""
        CREATE POLICY kb_delete ON knowledge_bases FOR DELETE
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships
            WHERE user_id = auth.uid() AND role IN ('admin','builder')
        ))
    """)
    op.execute("CREATE POLICY kb_service ON knowledge_bases USING (auth.role() = 'service_role')")

    op.create_table(
        "kb_documents",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "kb_id",
            UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "uploaded_by",
            UUID(as_uuid=True),
            sa.ForeignKey("auth.users.id"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("file_type", sa.String(10), nullable=False),
        sa.Column("file_size", sa.BigInteger, nullable=False),
        sa.Column("storage_path", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error_detail", sa.Text, nullable=True),
        sa.Column("page_count", sa.Integer, nullable=True),
        sa.Column("token_count", sa.Integer, nullable=True),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
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
            "file_type IN ('pdf','docx','txt','md','html')", name="kb_doc_valid_type"
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','ready','failed')", name="kb_doc_valid_status"
        ),
        sa.CheckConstraint("file_size > 0", name="kb_doc_positive_size"),
        schema="public",
    )
    # Primary access pattern: list docs in a KB ordered by upload time
    op.create_index(
        "ix_kb_documents_kb_status_created",
        "kb_documents",
        ["kb_id", "status", "created_at"],
    )
    op.create_index(
        "ix_kb_documents_workspace_created", "kb_documents", ["workspace_id", "created_at"]
    )
    # Dedup detection: fast lookup by content hash within a KB
    op.create_index("ix_kb_documents_hash", "kb_documents", ["kb_id", "content_hash"])

    op.execute("ALTER TABLE kb_documents ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY kb_doc_select ON kb_documents FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute("""
        CREATE POLICY kb_doc_insert ON kb_documents FOR INSERT
        WITH CHECK (workspace_id IN (
            SELECT workspace_id FROM memberships
            WHERE user_id = auth.uid() AND role IN ('admin','builder')
        ))
    """)
    op.execute("""
        CREATE POLICY kb_doc_delete ON kb_documents FOR DELETE
        USING (
            uploaded_by = auth.uid()
            OR workspace_id IN (
                SELECT workspace_id FROM memberships
                WHERE user_id = auth.uid() AND role = 'admin'
            )
        )
    """)
    op.execute("CREATE POLICY kb_doc_service ON kb_documents USING (auth.role() = 'service_role')")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS kb_doc_service ON kb_documents")
    op.execute("DROP POLICY IF EXISTS kb_doc_delete ON kb_documents")
    op.execute("DROP POLICY IF EXISTS kb_doc_insert ON kb_documents")
    op.execute("DROP POLICY IF EXISTS kb_doc_select ON kb_documents")
    op.drop_index("ix_kb_documents_hash", table_name="kb_documents")
    op.drop_index("ix_kb_documents_workspace_created", table_name="kb_documents")
    op.drop_index("ix_kb_documents_kb_status_created", table_name="kb_documents")
    op.drop_table("kb_documents")

    op.execute("DROP POLICY IF EXISTS kb_service ON knowledge_bases")
    op.execute("DROP POLICY IF EXISTS kb_delete ON knowledge_bases")
    op.execute("DROP POLICY IF EXISTS kb_insert ON knowledge_bases")
    op.execute("DROP POLICY IF EXISTS kb_select ON knowledge_bases")
    op.drop_index("ix_kbs_agent_id", table_name="knowledge_bases")
    op.drop_index("ix_kbs_workspace_id", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
