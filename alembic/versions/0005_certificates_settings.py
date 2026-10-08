"""Platform parameters and the certificates learners earn for completing a course or a learning path.

Additive only: two new tables. The previous app's `certificates` table stays as it is (no longer mapped).

Revision ID: 0005_certificates_settings
Revises: 0004_learning_paths
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_certificates_settings"
down_revision = "0004_learning_paths"
branch_labels = None
depends_on = None

_TIMESTAMP = sa.DateTime(timezone=True)
_NOW = sa.text("CURRENT_TIMESTAMP")
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TABLES = ("app_settings", "learner_certificates")


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("values", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
    )

    op.create_table(
        "learner_certificates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id", ondelete="SET NULL")),
        sa.Column("path_id", sa.Integer(), sa.ForeignKey("learning_paths.id", ondelete="SET NULL")),
        sa.Column("recipient_name", sa.String(200), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("details", _JSON),
        sa.Column("score", sa.Float()),
        sa.Column("issued_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("expires_at", _TIMESTAMP),
        sa.Column("revoked_at", _TIMESTAMP),
        sa.Column("revoked_reason", sa.Text()),
        sa.UniqueConstraint("code", name="uq_learner_certificates_code"),
    )
    for column in ("id", "user_id", "course_id", "path_id"):
        op.create_index(f"ix_learner_certificates_{column}", "learner_certificates", [column])
    # At most one certificate in force (not revoked) per person and course, and per person and path.
    for kind, column in (("course", "course_id"), ("path", "path_id")):
        in_force = sa.text(f"kind = '{kind}' AND revoked_at IS NULL")
        op.create_index(
            f"uq_learner_certificates_{kind}", "learner_certificates", ["user_id", column], unique=True,
            postgresql_where=in_force, sqlite_where=in_force,
        )

    if op.get_bind().dialect.name == "postgresql":
        # Like every other table here: nothing is reachable through Supabase's public REST API.
        for table in _TABLES:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("learner_certificates")
    op.drop_table("app_settings")
