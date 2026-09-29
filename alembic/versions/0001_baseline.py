"""Baseline: the schema production had before Alembic (created by create_all plus ad-hoc ALTERs).

Verified against production on 2026-09-28. Production is stamped at this revision by
`app.db.migrate` instead of running it; fresh databases (development, tests) run it.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def _id_index(table: str) -> None:
    op.create_index(f"ix_{table}_id", table, ["id"])


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(200), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("position", sa.String(200)),
        sa.Column("department", sa.String(200)),
        sa.Column("hire_date", sa.Date()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("password_hash", sa.String(300), server_default=""),
        sa.Column("reports_to", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("permissions_json", sa.Text(), server_default="[]"),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
        sa.Column("preferred_language", sa.String(5), server_default="es"),
    )
    _id_index("users")
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "courses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("language", sa.String(5), server_default="es"),
    )
    _id_index("courses")

    op.create_table(
        "modules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("content_text", sa.Text()),
        sa.Column("video_url", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("audio_url", sa.String(500), server_default=""),
        sa.Column("generation_status", sa.String(50), server_default="pending"),
    )
    _id_index("modules")

    op.create_table(
        "evaluations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("module_id", sa.Integer(), sa.ForeignKey("modules.id"), nullable=False, unique=True),
        sa.Column("questions_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("max_attempts", sa.Integer(), server_default="3"),
    )
    _id_index("evaluations")

    op.create_table(
        "enrollments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("progress_pct", sa.Float()),
        sa.Column("enrolled_at", sa.DateTime(), server_default=sa.func.now()),
    )
    _id_index("enrollments")

    op.create_table(
        "evaluation_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("evaluation_id", sa.Integer(), sa.ForeignKey("evaluations.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("enrollments.id"), nullable=False),
        sa.Column("module_id", sa.Integer(), sa.ForeignKey("modules.id"), nullable=False),
        sa.Column("answers_json", sa.Text()),
        sa.Column("score", sa.Float()),
        sa.Column("passed", sa.Boolean()),
        sa.Column("attempt_number", sa.Integer()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    _id_index("evaluation_attempts")

    op.create_table(
        "module_progress",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("enrollments.id"), nullable=False),
        sa.Column("module_id", sa.Integer(), sa.ForeignKey("modules.id"), nullable=False),
        sa.Column("completed", sa.Boolean()),
        sa.Column("score", sa.Float()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("attempts", sa.Integer(), server_default="0"),
        sa.Column("passed", sa.Boolean(), server_default=sa.false()),
    )
    _id_index("module_progress")

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token", sa.String(300), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    _id_index("refresh_tokens")
    op.create_index("ix_refresh_tokens_token", "refresh_tokens", ["token"], unique=True)

    op.create_table(
        "user_videos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("module_id", sa.Integer(), sa.ForeignKey("modules.id")),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("storage_url", sa.String(500), nullable=False),
        sa.Column("duration", sa.Integer()),
        sa.Column("file_size", sa.Integer()),
        sa.Column("format", sa.String(20)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("status", sa.String(20)),
    )
    _id_index("user_videos")


def downgrade() -> None:
    # Production was stamped at this revision, never created by it: its data and the tables of other
    # applications (with foreign keys into these) must never be dropped by `alembic downgrade base`.
    raise NotImplementedError("the baseline cannot be downgraded")
