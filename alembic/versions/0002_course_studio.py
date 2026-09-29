"""Course studio: media assets, background jobs and the columns the new flows need.

Additive only (no drops or renames), so the previous release keeps working on this schema.

Revision ID: 0002_course_studio
Revises: 0001_baseline
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_course_studio"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

_TIMESTAMP = sa.DateTime(timezone=True)
_NOW = sa.text("CURRENT_TIMESTAMP")
# JSONB on Postgres: JSON has no equality operator (DISTINCT, =, GROUP BY fail) and no GIN indexes.
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_ASSET_COLUMNS = ("video_asset_id", "poster_asset_id", "captions_asset_id", "document_asset_id")


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("bucket", sa.String(100), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("original_filename", sa.String(300)),
        sa.Column("meta", _JSON),
        sa.Column("error", sa.Text()),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("course_id", sa.Integer()),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW),
    )
    op.create_index("ix_media_assets_course_id", "media_assets", ["course_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("type", sa.String(60), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("payload", _JSON),
        sa.Column("state", _JSON),
        sa.Column("result", _JSON),
        sa.Column("error", sa.Text()),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("step", sa.String(160), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("run_after", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("locked_until", _TIMESTAMP),
        sa.Column("locked_by", sa.String(100)),
        sa.Column("dedupe_key", sa.String(200)),
        sa.Column("course_id", sa.Integer()),
        sa.Column("module_id", sa.Integer()),
        sa.Column("created_by", sa.Integer()),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW),
        sa.Column("started_at", _TIMESTAMP),
        sa.Column("finished_at", _TIMESTAMP),
    )
    for column in ("type", "dedupe_key", "course_id", "module_id"):
        op.create_index(f"ix_jobs_{column}", "jobs", [column])
    op.create_index("ix_jobs_claim", "jobs", ["status", "run_after"])
    # At most one active job per dedupe key, even when two containers enqueue at the same time.
    active = sa.text("status IN ('queued', 'running')")
    op.create_index(
        "uq_jobs_active_dedupe_key", "jobs", ["dedupe_key"], unique=True, postgresql_where=active, sqlite_where=active
    )

    with op.batch_alter_table("courses") as batch:
        batch.add_column(sa.Column("source", sa.String(20), nullable=False, server_default="manual"))
        batch.add_column(sa.Column("settings", _JSON))
        batch.add_column(sa.Column("cover_asset_id", sa.String(36)))
        batch.add_column(sa.Column("published_at", _TIMESTAMP))
        batch.add_column(sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW))
        batch.create_foreign_key(
            "fk_courses_cover_asset", "media_assets", ["cover_asset_id"], ["id"], ondelete="SET NULL"
        )

    with op.batch_alter_table("modules") as batch:
        batch.add_column(sa.Column("description", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("source", sa.String(20), nullable=False, server_default="text"))
        batch.add_column(sa.Column("storyboard", _JSON))
        for column in _ASSET_COLUMNS:
            batch.add_column(sa.Column(column, sa.String(36)))
            batch.create_foreign_key(f"fk_modules_{column}", "media_assets", [column], ["id"], ondelete="SET NULL")
        batch.add_column(sa.Column("duration_seconds", sa.Float()))
        batch.add_column(sa.Column("generation_error", sa.Text()))
        batch.add_column(sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW))

    with op.batch_alter_table("evaluations") as batch:
        batch.add_column(sa.Column("spec", _JSON))
        batch.add_column(sa.Column("passing_score", sa.Integer(), nullable=False, server_default="70"))
        batch.add_column(sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=_NOW))

    with op.batch_alter_table("evaluation_attempts") as batch:
        batch.add_column(sa.Column("results", _JSON))

    _fail_on_duplicate_enrollments()
    _merge_duplicate_progress()

    with op.batch_alter_table("enrollments") as batch:
        batch.add_column(sa.Column("completed_at", _TIMESTAMP))
        batch.add_column(sa.Column("assigned_by", sa.Integer()))
        batch.create_foreign_key("fk_enrollments_assigned_by", "users", ["assigned_by"], ["id"], ondelete="SET NULL")
        batch.create_unique_constraint("uq_enrollments_user_course", ["user_id", "course_id"])

    with op.batch_alter_table("module_progress") as batch:
        batch.add_column(sa.Column("last_position_seconds", sa.Float(), nullable=False, server_default="0"))
        batch.create_unique_constraint("uq_module_progress_enrollment_module", ["enrollment_id", "module_id"])

    _classify_existing_content()

    # Refresh tokens issued before B1 were stored in plain text (and some were committed to a
    # public repository). They are hashed now; revoke every token issued until today.
    op.execute(sa.text("UPDATE refresh_tokens SET revoked = :revoked").bindparams(revoked=True))

    if op.get_bind().dialect.name == "postgresql":
        # Like every other table here: nothing is reachable through Supabase's public REST API.
        for table in ("media_assets", "jobs", op.get_context().version_table):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def _classify_existing_content() -> None:
    """Mark what the previous app generated with AI, so the new editor treats it as such.

    A module's own video decides first; only modules without one inherit their course's source
    (the previous app let admins replace an AI video by hand, or generate one inside a manual course).
    """
    op.execute(
        "UPDATE modules SET source = 'ai' "
        "WHERE video_url LIKE 'heygen://%' OR video_url LIKE '%.heygen.ai/%' "
        "OR video_url LIKE '%/storage/v1/object/public/course-videos/%'"
    )
    op.execute("UPDATE modules SET source = 'upload' WHERE source = 'text' AND video_url LIKE 'http%'")
    op.execute(
        "UPDATE courses SET source = 'ai' "
        "WHERE description LIKE 'Curso generado con IA%' "
        "OR id IN (SELECT course_id FROM modules WHERE source = 'ai')"
    )
    op.execute(
        "UPDATE modules SET source = 'ai' "
        "WHERE source = 'text' AND course_id IN (SELECT id FROM courses WHERE source = 'ai')"
    )


def _lock_against_writes(table: str) -> None:
    # The previous release keeps writing during the deploy: no new rows until the constraint exists.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"LOCK TABLE {table} IN EXCLUSIVE MODE")


def _fail_on_duplicate_enrollments() -> None:
    """Merging enrollments would mean re-pointing module_progress, attempts and legacy certificates."""
    _lock_against_writes("enrollments")
    if op.get_context().as_sql:  # offline --sql: nothing to inspect
        return
    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT user_id, course_id, count(*) FROM enrollments "
            "GROUP BY user_id, course_id HAVING count(*) > 1 ORDER BY user_id, course_id"
        )
    ).all()
    if duplicates:
        pairs = ", ".join(f"(user {u}, course {c}) x{n}" for u, c, n in duplicates)
        raise RuntimeError(
            f"enrollments has duplicate (user_id, course_id) pairs: {pairs}. Merge them by hand "
            "(module_progress, evaluation_attempts and certificates reference enrollments.id), then redeploy."
        )


def _merge_duplicate_progress() -> None:
    """The previous app could insert the same (enrollment, module) twice: keep one row with the best progress."""
    _lock_against_writes("module_progress")
    any_true = "bool_or" if op.get_bind().dialect.name == "postgresql" else "max"
    op.execute(
        f"""
        UPDATE module_progress SET
            completed = merged.completed, passed = merged.passed, score = merged.score,
            attempts = merged.attempts, completed_at = merged.completed_at
        FROM (
            SELECT min(id) AS keep_id,
                   {any_true}(coalesce(completed, false)) AS completed,
                   {any_true}(coalesce(passed, false)) AS passed,
                   max(score) AS score, max(attempts) AS attempts, min(completed_at) AS completed_at
            FROM module_progress GROUP BY enrollment_id, module_id HAVING count(*) > 1
        ) AS merged
        WHERE module_progress.id = merged.keep_id
        """
    )
    op.execute(
        "DELETE FROM module_progress WHERE id NOT IN "
        "(SELECT min(id) FROM module_progress GROUP BY enrollment_id, module_id)"
    )


def downgrade() -> None:
    with op.batch_alter_table("module_progress") as batch:
        batch.drop_constraint("uq_module_progress_enrollment_module", type_="unique")
        batch.drop_column("last_position_seconds")
    with op.batch_alter_table("enrollments") as batch:
        batch.drop_constraint("uq_enrollments_user_course", type_="unique")
        batch.drop_constraint("fk_enrollments_assigned_by", type_="foreignkey")
        batch.drop_column("assigned_by")
        batch.drop_column("completed_at")
    with op.batch_alter_table("evaluation_attempts") as batch:
        batch.drop_column("results")
    with op.batch_alter_table("evaluations") as batch:
        for column in ("updated_at", "passing_score", "spec"):
            batch.drop_column(column)
    with op.batch_alter_table("modules") as batch:
        for column in _ASSET_COLUMNS:
            batch.drop_constraint(f"fk_modules_{column}", type_="foreignkey")
            batch.drop_column(column)
        for column in ("updated_at", "generation_error", "duration_seconds", "storyboard", "source", "description"):
            batch.drop_column(column)
    with op.batch_alter_table("courses") as batch:
        batch.drop_constraint("fk_courses_cover_asset", type_="foreignkey")
        for column in ("updated_at", "published_at", "cover_asset_id", "settings", "source"):
            batch.drop_column(column)
    op.drop_table("jobs")
    op.drop_table("media_assets")
