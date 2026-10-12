"""Create kb_chunks table with pgvector HNSW index and RLS

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-20

Complexity: HNSW insert O(log n); cosine similarity search O(log n).
n = vectors in the index per workspace KB.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # vector extension already enabled in 0011
    op.create_table(
        "kb_chunks",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            nullable=False,
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
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        # BGE-M3 produces 1024-dimensional embeddings; NULL until embedding job completes
        sa.Column(
            "embedding",
            sa.Text,  # stored as pgvector type via raw SQL below; ORM uses Text as proxy
            nullable=True,
        ),
        sa.Column("token_count", sa.Integer, nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunk"),
        sa.CheckConstraint("token_count > 0", name="kb_chunk_positive_tokens"),
        schema="public",
    )

    # Change embedding column to proper pgvector type (vector(1024))
    op.execute("ALTER TABLE kb_chunks ALTER COLUMN embedding TYPE vector(1024) USING NULL")

    # HNSW index: m=16 ef_construction=64 — good defaults for datasets up to ~1M vectors
    # Cosine distance because BGE-M3 embeddings are L2-normalised
    op.execute("""
        CREATE INDEX ix_kb_chunks_embedding_hnsw
        ON kb_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)
    # Filtered vector search (WHERE kb_id = ?) uses this btree index first
    op.create_index("ix_kb_chunks_kb_id", "kb_chunks", ["kb_id"])
    op.create_index("ix_kb_chunks_document_id", "kb_chunks", ["document_id"])

    op.execute("ALTER TABLE kb_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY kb_chunk_select ON kb_chunks FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
    """)
    op.execute("CREATE POLICY kb_chunk_service ON kb_chunks USING (auth.role() = 'service_role')")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS kb_chunk_service ON kb_chunks")
    op.execute("DROP POLICY IF EXISTS kb_chunk_select ON kb_chunks")
    op.drop_index("ix_kb_chunks_document_id", table_name="kb_chunks")
    op.drop_index("ix_kb_chunks_kb_id", table_name="kb_chunks")
    op.execute("DROP INDEX IF EXISTS ix_kb_chunks_embedding_hnsw")
    op.drop_table("kb_chunks")
