"""Interactive activities after a module's video, and the learners' attempts.

Additive only: two new tables.

Revision ID: 0003_activities
Revises: 0002_course_studio
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_activities"
down_revision = "0002_course_studio"
branch_labels = None
depends_on = None

_TIMESTAMP = sa.DateTime(timezone=True)
_NOW = sa.text("CURRENT_TIMESTAMP")
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "activities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("module_id", sa.Integer(), sa.ForeignKey("modules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("intro", sa.Text(), nullable=False, server_default=""),
        sa.Column("spec", _JSON, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("image_asset_id", sa.String(36), sa.ForeignKey("media_assets.id", ondelete="SET NULL")),
        sa.Column("narration", _JSON),
        sa.Column("voice_status", sa.String(20), nullable=False, server_default="none"),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW),
    )
    op.create_index("ix_activities_id", "activities", ["id"])
    op.create_index("ix_activities_module_id", "activities", ["module_id"])
    op.create_table(
        "activity_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("activity_id", sa.Integer(), sa.ForeignKey("activities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("enrollments.id", ondelete="CASCADE")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", _JSON, nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("finished_at", _TIMESTAMP),
    )
    op.create_index("ix_activity_attempts_id", "activity_attempts", ["id"])
    op.create_index("ix_activity_attempts_enrollment_activity", "activity_attempts", ["enrollment_id", "activity_id"])

    if op.get_bind().dialect.name == "postgresql":
        # Like every other table here: nothing is reachable through Supabase's public REST API.
        for table in ("activities", "activity_attempts"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("activity_attempts")
    op.drop_table("activities")
