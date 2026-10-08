"""Profiles and learning paths: ordered courses per profile, who takes each path, and each person's profile.

Additive only: five new tables and a nullable column on users.

Revision ID: 0004_learning_paths
Revises: 0003_activities
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_learning_paths"
down_revision = "0003_activities"
branch_labels = None
depends_on = None

_TIMESTAMP = sa.DateTime(timezone=True)
_NOW = sa.text("CURRENT_TIMESTAMP")
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TABLES = ("job_profiles", "learning_paths", "learning_path_profiles", "learning_path_courses", "path_assignments")


def upgrade() -> None:
    op.create_table(
        "job_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.UniqueConstraint("name", name="uq_job_profiles_name"),
    )
    op.create_index("ix_job_profiles_id", "job_profiles", ["id"])

    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("profile_id", sa.Integer()))
        batch.create_foreign_key("fk_users_profile_id", "job_profiles", ["profile_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_users_profile_id", ["profile_id"])

    op.create_table(
        "learning_paths",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("settings", _JSON),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("published_at", _TIMESTAMP),
    )
    op.create_index("ix_learning_paths_id", "learning_paths", ["id"])

    op.create_table(
        "learning_path_profiles",
        sa.Column("path_id", sa.Integer(), sa.ForeignKey("learning_paths.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("job_profiles.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_learning_path_profiles_profile_id", "learning_path_profiles", ["profile_id"])

    op.create_table(
        "learning_path_courses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("path_id", sa.Integer(), sa.ForeignKey("learning_paths.id", ondelete="CASCADE"), nullable=False),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("path_id", "course_id", name="uq_learning_path_courses_path_course"),
    )
    op.create_index("ix_learning_path_courses_id", "learning_path_courses", ["id"])
    op.create_index("ix_learning_path_courses_course_id", "learning_path_courses", ["course_id"])

    op.create_table(
        "path_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("path_id", sa.Integer(), sa.ForeignKey("learning_paths.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("assigned_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("assigned_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("due_at", _TIMESTAMP),
        sa.Column("completed_at", _TIMESTAMP),
        sa.UniqueConstraint("path_id", "user_id", name="uq_path_assignments_path_user"),
    )
    op.create_index("ix_path_assignments_id", "path_assignments", ["id"])
    op.create_index("ix_path_assignments_user_id", "path_assignments", ["user_id"])

    if op.get_bind().dialect.name == "postgresql":
        # Like every other table here: nothing is reachable through Supabase's public REST API.
        for table in _TABLES:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("path_assignments")
    op.drop_table("learning_path_courses")
    op.drop_table("learning_path_profiles")
    op.drop_table("learning_paths")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_profile_id")
        batch.drop_constraint("fk_users_profile_id", type_="foreignkey")
        batch.drop_column("profile_id")
    op.drop_table("job_profiles")
