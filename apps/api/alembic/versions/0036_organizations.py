"""Organizations — a tenancy layer above workspaces (Bazaar provenance).

Introduces `organizations` and `workspaces.organization_id` so the Bazaar can
label an agent's provenance: My Agents / Shared with me / My Org / Third-party.
An org groups a person's workspaces; org NAMES surface as public provenance
labels in the platform-wide Bazaar, so RLS allows any authenticated read of
(id, name, slug) but only the service_role (API) writes.

Backfill: one organization per DISTINCT workspace owner (grouping a person's
workspaces), named from the owner's profile. Every existing workspace is
attached to its owner's org, then organization_id is set NOT NULL.

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-30
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False, unique=True),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("auth.users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
        ),
        # One org per owner (backfill relies on this; the service upserts by owner).
        sa.UniqueConstraint("owner_id", name="uq_organization_owner"),
    )

    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    # Org names are public provenance labels in the global Bazaar → any authenticated
    # session may read them; only the API (service_role) creates/renames.
    op.execute("""
        CREATE POLICY organizations_select ON organizations FOR SELECT
        USING (auth.role() IN ('authenticated', 'service_role'))
    """)
    op.execute("""
        CREATE POLICY organizations_service ON organizations
        USING (auth.role() = 'service_role')
    """)

    # The FK on workspaces — nullable while we backfill.
    op.add_column(
        "workspaces",
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )

    # Backfill: one org per distinct workspace owner, named from the owner's profile.
    op.execute("""
        INSERT INTO organizations (id, name, slug, owner_id)
        SELECT gen_random_uuid(),
               COALESCE(NULLIF(u.raw_user_meta_data->>'full_name', ''),
                        split_part(u.email, '@', 1)) || ' Organization',
               'org-' || substr(o.owner_id::text, 1, 8),
               o.owner_id
        FROM (SELECT DISTINCT owner_id FROM workspaces) o
        JOIN auth.users u ON u.id = o.owner_id
    """)
    op.execute("""
        UPDATE workspaces w
        SET organization_id = org.id
        FROM organizations org
        WHERE org.owner_id = w.owner_id
    """)

    op.alter_column("workspaces", "organization_id", nullable=False)
    op.create_index("ix_workspaces_organization_id", "workspaces", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_workspaces_organization_id", table_name="workspaces")
    op.drop_column("workspaces", "organization_id")
    for policy in ("organizations_service", "organizations_select"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON organizations")
    op.drop_table("organizations")
