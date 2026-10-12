"""audit_log.actor_workspace_id FK ON DELETE SET NULL

Allows deleting a workspace without losing its audit history.
The workspace_id reference is nulled out; the action / resource_id / metadata remain.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-21
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("audit_log_actor_workspace_id_fkey", "audit_log", type_="foreignkey")
    op.create_foreign_key(
        "audit_log_actor_workspace_id_fkey",
        "audit_log",
        "workspaces",
        ["actor_workspace_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("audit_log_actor_workspace_id_fkey", "audit_log", type_="foreignkey")
    op.create_foreign_key(
        "audit_log_actor_workspace_id_fkey",
        "audit_log",
        "workspaces",
        ["actor_workspace_id"],
        ["id"],
    )
