"""Revisions of a module's script: the changes the AI proposes from an admin's observation, and every change applied
to it (proposed, saved by hand, rewritten or undone) with the content before and after, so it can be undone.

Additive only: one new table.

Revision ID: 0007_module_revisions
Revises: 0006_points_ranking
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_module_revisions"
down_revision = "0006_points_ranking"
branch_labels = None
depends_on = None

_TIMESTAMP = sa.DateTime(timezone=True)
_NOW = sa.text("CURRENT_TIMESTAMP")
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "module_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("module_id", sa.Integer(), sa.ForeignKey("modules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target", sa.String(10), nullable=False, server_default="script"),
        sa.Column("activity_id", sa.Integer(), sa.ForeignKey("activities.id", ondelete="CASCADE")),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("observation", sa.Text(), nullable=False, server_default=""),
        sa.Column("scope", _JSON),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("unresolved", sa.Text(), nullable=False, server_default=""),
        sa.Column("base_hash", sa.String(64)),
        sa.Column("before", _JSON),
        sa.Column("after", _JSON),
        sa.Column("changes", _JSON),
        sa.Column("job_id", sa.String(36)),
        sa.Column("error", sa.Text()),
        sa.Column("usage", _JSON),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("applied_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("applied_at", _TIMESTAMP),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW),
    )
    op.create_index("ix_module_revisions_id", "module_revisions", ["id"])
    op.create_index("ix_module_revisions_module_created", "module_revisions", ["module_id", "created_at"])
    op.create_index("ix_module_revisions_activity_id", "module_revisions", ["activity_id"])

    if op.get_bind().dialect.name == "postgresql":
        # Like every other table here: nothing is reachable through Supabase's public REST API.
        op.execute("ALTER TABLE module_revisions ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("module_revisions")
