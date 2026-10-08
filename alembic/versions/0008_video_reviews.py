"""The automatic review of the modules' AI videos: frames taken from each video, what its own checks and the AI found
in them, and what the admin did with each finding.

Additive only: one new table.

Revision ID: 0008_video_reviews
Revises: 0007_module_revisions
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_video_reviews"
down_revision = "0007_module_revisions"
branch_labels = None
depends_on = None

_TIMESTAMP = sa.DateTime(timezone=True)
_NOW = sa.text("CURRENT_TIMESTAMP")
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "video_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("module_id", sa.Integer(), sa.ForeignKey("modules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("video_asset_id", sa.String(36), sa.ForeignKey("media_assets.id", ondelete="SET NULL")),
        sa.Column("render_job_id", sa.String(36)),
        sa.Column("job_id", sa.String(36)),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("verdict", sa.String(20)),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text()),
        sa.Column("issues", _JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("frames", _JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("checks", _JSON),
        sa.Column("model", sa.String(100)),
        sa.Column("usage", _JSON),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("finished_at", _TIMESTAMP),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW),
    )
    for column in ("id", "module_id", "video_asset_id"):
        op.create_index(f"ix_video_reviews_{column}", "video_reviews", [column])

    if op.get_bind().dialect.name == "postgresql":
        # Like every other table here: nothing is reachable through Supabase's public REST API.
        op.execute("ALTER TABLE video_reviews ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("video_reviews")
