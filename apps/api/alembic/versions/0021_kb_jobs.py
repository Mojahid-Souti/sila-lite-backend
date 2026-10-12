"""Create kb_jobs table — durable queue for out-of-process document processing.

The API enqueues one row per upload (status='queued') and NOTIFYs 'kb_jobs';
the dedicated worker claims rows with FOR UPDATE SKIP LOCKED and runs the
parse->embed pipeline off the API event loop. RLS mirrors kb_documents.

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-03
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_jobs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
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
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('queued','processing','done','failed')", name="kb_job_valid_status"
        ),
        sa.CheckConstraint("attempts >= 0", name="kb_job_attempts_nonneg"),
    )
    # Claim index: the worker scans queued rows oldest-first.
    op.create_index("ix_kb_jobs_claim", "kb_jobs", ["status", "created_at"])
    op.create_index("ix_kb_jobs_document", "kb_jobs", ["document_id"])

    # RLS — internal queue. The worker/API use the service role; a workspace-member
    # SELECT policy is provided for a future job-status UI.
    op.execute("ALTER TABLE kb_jobs ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY kb_job_select ON kb_jobs FOR SELECT
        USING (workspace_id IN (
            SELECT workspace_id FROM memberships WHERE user_id = auth.uid()
        ))
        """
    )
    op.execute("CREATE POLICY kb_job_service ON kb_jobs USING (auth.role() = 'service_role')")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS kb_job_service ON kb_jobs")
    op.execute("DROP POLICY IF EXISTS kb_job_select ON kb_jobs")
    op.drop_index("ix_kb_jobs_document", table_name="kb_jobs")
    op.drop_index("ix_kb_jobs_claim", table_name="kb_jobs")
    op.drop_table("kb_jobs")
