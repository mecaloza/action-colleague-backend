"""Points and rankings: the ledger of what learners earn, and the indexes a person's overview and the rankings read by.

Additive only: one new table, and three indexes on the attempt tables.

Revision ID: 0006_points_ranking
Revises: 0005_certificates_settings
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_points_ranking"
down_revision = "0005_certificates_settings"
branch_labels = None
depends_on = None

_TIMESTAMP = sa.DateTime(timezone=True)
_NOW = sa.text("CURRENT_TIMESTAMP")
# The attempts a person's overview lists newest first, and the rankings' past (`points.backfill`) reads.
_ATTEMPT_INDEXES = (
    ("ix_evaluation_attempts_enrollment_id", "evaluation_attempts", ["enrollment_id"]),
    ("ix_evaluation_attempts_user_created", "evaluation_attempts", ["user_id", "created_at"]),
    ("ix_activity_attempts_user_finished", "activity_attempts", ["user_id", "finished_at"]),
)


def upgrade() -> None:
    op.create_table(
        "learner_points",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id", ondelete="CASCADE")),
        sa.Column("path_id", sa.Integer(), sa.ForeignKey("learning_paths.id", ondelete="CASCADE")),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("source_key", sa.String(80), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("earned_at", _TIMESTAMP, nullable=False, server_default=_NOW),
    )
    op.create_index("ix_learner_points_id", "learner_points", ["id"])
    op.create_index("ix_learner_points_earned", "learner_points", ["earned_at"])  # the ranking of every course
    for column in ("user", "course", "path"):
        op.create_index(f"ix_learner_points_{column}_earned", "learner_points", [f"{column}_id", "earned_at"])
    # A module, course or path completed earns its points once per person.
    once = sa.text("source IN ('module', 'course', 'path')")
    op.create_index(
        "uq_learner_points_milestone", "learner_points", ["user_id", "source_key"], unique=True,
        postgresql_where=once, sqlite_where=once,
    )
    for name, table, columns in _ATTEMPT_INDEXES:
        op.create_index(name, table, columns)

    if op.get_bind().dialect.name == "postgresql":
        # Like every other table here: nothing is reachable through Supabase's public REST API.
        op.execute("ALTER TABLE learner_points ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    for name, table, _ in reversed(_ATTEMPT_INDEXES):
        op.drop_index(name, table_name=table)
    op.drop_table("learner_points")
