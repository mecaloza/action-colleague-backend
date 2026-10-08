"""
Certificates: earned by completing a course or a learning path, verified by anyone with their code, drawn as a PDF on
demand (`certificate_pdf`).

- Issued by themselves, at most once per person and course (or path): when an enrollment becomes completed (if the
  platform issues them, or the course says so, and the learner's average best quiz score reaches the platform's
  minimum, when there is one), and when a path assignment does (if the platform issues them and the path gives one).
  A course reopened later (a module added) never takes one back, and one revoked is not issued again by itself:
  admins issue them by hand, revoke and reissue them.
- A certificate is a snapshot: who, what, hours, score, issuer and signer as they were. Later edits never change it.
- One read and one insert, whatever the number of people completing at once (an admin's change can complete a course
  for hundreds).
"""

import secrets
from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    Certificate,
    Course,
    Enrollment,
    LearningPath,
    LearningPathCourse,
    Module,
    ModuleProgress,
    PathAssignment,
    User,
)
from app.schemas.certificates import (
    AdminCertificate,
    CertificateDetails,
    CertificateRef,
    LearnerCertificate,
    PublicCertificate,
)
from app.schemas.paths import PathSettings
from app.schemas.settings import PlatformSettings
from app.services import settings as platform

# Crockford's base 32: no I, L, O or U, so a code read aloud or typed by hand can't be misread.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MISREAD = str.maketrans("OIL", "011")
DEFAULT_ISSUER = "Rubinstein Colleague"
_BATCH = 500  # rows per insert: far below the database's limit of parameters per statement


class CertificateError(Exception):
    """Can't be done as asked; the message is the admin's."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    """SQLite hands timezone-aware columns back naive (they are UTC): comparable again."""
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def new_code() -> str:
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"RC-{body[:4]}-{body[4:]}"


def normalize_code(raw: str) -> str | None:
    """A code as someone typed it (any case, with or without dashes, O for 0, I or L for 1), or None if it can't
    be one."""
    compact = "".join(raw.split()).replace("-", "").upper()
    if len(compact) == 10 and compact.startswith("RC"):
        compact = compact[2:]
    body = compact.translate(_MISREAD)
    if len(body) != 8 or any(char not in CODE_ALPHABET for char in body):
        return None
    return f"RC-{body[:4]}-{body[4:]}"


def add_months(moment: datetime, months: int) -> datetime:
    """The same day `months` later (or the month's last day: 31 Jan + 1 month = 28/29 Feb)."""
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    return moment.replace(year=year, month=month, day=min(moment.day, monthrange(year, month)[1]))


def status_of(certificate: Certificate, now: datetime | None = None) -> str:
    if certificate.revoked_at is not None:
        return "revoked"
    expires_at = aware(certificate.expires_at)
    return "expired" if expires_at is not None and expires_at <= (now or utcnow()) else "valid"


def details_of(certificate: Certificate) -> CertificateDetails:
    try:
        return CertificateDetails.model_validate(certificate.details or {})
    except ValidationError:  # edited by hand: what is left of it, never a broken certificate
        return CertificateDetails()


def issuer_name(certificate: Certificate) -> str:
    return details_of(certificate).company_name or DEFAULT_ISSUER


# ── Links and views ───────────────────────────────────────────────────


def pdf_url(code: str) -> str:
    return f"{get_settings().public_api_url.rstrip('/')}/public/certificates/{code}/pdf"


def verify_url(code: str) -> str:
    return f"{get_settings().public_app_url.rstrip('/')}/verify/{code}"


def ref(certificate: Certificate) -> CertificateRef:
    code = certificate.code
    return CertificateRef(id=certificate.id, code=code, pdf_url=pdf_url(code), verify_url=verify_url(code))


def _learner_fields(certificate: Certificate, now: datetime) -> dict[str, Any]:
    code = certificate.code
    return {
        "id": certificate.id,
        "code": code,
        "kind": certificate.kind,
        "title": certificate.title,
        "issued_at": certificate.issued_at,
        "expires_at": certificate.expires_at,
        "status": status_of(certificate, now),
        "course_id": certificate.course_id,
        "path_id": certificate.path_id,
        "score": certificate.score,
        "pdf_url": pdf_url(code),
        "verify_url": verify_url(code),
    }


def learner_view(certificate: Certificate, now: datetime) -> LearnerCertificate:
    return LearnerCertificate(**_learner_fields(certificate, now))


def admin_view(certificate: Certificate, now: datetime) -> AdminCertificate:
    return AdminCertificate(
        **_learner_fields(certificate, now),
        recipient_name=certificate.recipient_name,
        user_id=certificate.user_id,
        revoked_at=certificate.revoked_at,
        revoked_reason=certificate.revoked_reason,
    )


def public_view(certificate: Certificate, now: datetime) -> PublicCertificate:
    return PublicCertificate(
        code=certificate.code,
        status=status_of(certificate, now),
        kind=certificate.kind,
        recipient_name=certificate.recipient_name,
        title=certificate.title,
        issued_at=certificate.issued_at,
        expires_at=certificate.expires_at,
        issuer_name=issuer_name(certificate),
        details=details_of(certificate),
        pdf_url=pdf_url(certificate.code),
    )


@dataclass
class LearnerRefs:
    """A person's certificates in force (valid or expired), by course and by path."""

    courses: dict[int, CertificateRef]
    paths: dict[int, CertificateRef]


def learner_refs(
    db: Session, user_id: int, *, course_ids: Iterable[int] = (), path_ids: Iterable[int] = ()
) -> LearnerRefs:
    """One read, none when nothing is asked for."""
    course_ids, path_ids = list(course_ids), list(path_ids)
    refs = LearnerRefs({}, {})
    if not course_ids and not path_ids:
        return refs
    rows = db.query(Certificate).filter(
        Certificate.user_id == user_id,
        Certificate.revoked_at.is_(None),
        or_(
            and_(Certificate.kind == "course", Certificate.course_id.in_(course_ids)),
            and_(Certificate.kind == "path", Certificate.path_id.in_(path_ids)),
        ),
    )
    for certificate in rows:
        if certificate.kind == "course":
            refs.courses[certificate.course_id] = ref(certificate)
        else:
            refs.paths[certificate.path_id] = ref(certificate)
    return refs


# ── Snapshots ─────────────────────────────────────────────────────────


@dataclass
class _CourseFacts:
    title: str
    seconds: float  # of its videos
    modules: int
    completed_at: datetime | None  # the person's completion
    score_total: float  # the person's best quiz scores in it, added up
    score_count: int


@dataclass
class _Grant:
    """A certificate to write."""

    kind: str
    user_id: int
    course_id: int | None
    path_id: int | None
    recipient_name: str
    title: str
    details: dict[str, Any]
    score: float | None


def _course_columns():
    """Correlated, per course: the seconds of its videos and its number of modules."""
    seconds = select(func.coalesce(func.sum(Module.duration_seconds), 0.0)).where(Module.course_id == Course.id)
    modules = select(func.count(Module.id)).where(Module.course_id == Course.id)
    return seconds.correlate(Course).scalar_subquery(), modules.correlate(Course).scalar_subquery()


def _score_columns():
    """Correlated, per enrollment: its best quiz scores (one per module whose quiz was taken), added up and counted.
    Only a quiz submission sets a module's score; deleting the quiz clears it."""
    taken = ModuleProgress.enrollment_id == Enrollment.id
    total = select(func.coalesce(func.sum(ModuleProgress.score), 0.0)).where(taken)
    count = select(func.count(ModuleProgress.score)).where(taken)
    return total.correlate(Enrollment).scalar_subquery(), count.correlate(Enrollment).scalar_subquery()


def _snapshot(settings: PlatformSettings, courses: list[_CourseFacts]) -> tuple[dict[str, Any], float | None]:
    """The certificate's details and its score (the average best quiz score over every quiz taken, or None)."""
    taken = sum(course.score_count for course in courses)
    average = round(sum(course.score_total for course in courses) / taken, 1) if taken else None
    details = CertificateDetails(
        hours=round(sum(course.seconds for course in courses) / 3600, 2),
        modules=sum(course.modules for course in courses),
        courses=[{"title": course.title, "completed_at": course.completed_at} for course in courses],
        average_score=average,
        company_name=settings.general.company_name,
        signer_name=settings.certificates.signer_name,
        signer_title=settings.certificates.signer_title,
    )
    return details.model_dump(mode="json"), average


def _course_rows(db: Session, *conditions):
    """Per enrollment meeting `conditions`: who, the course and the facts of its snapshot. One read."""
    seconds, modules = _course_columns()
    total, count = _score_columns()
    return db.execute(
        select(
            Enrollment.user_id, Enrollment.course_id, Enrollment.completed_at, User.name, Course.title,
            Course.settings, seconds, modules, total, count,
        )
        .join(User, User.id == Enrollment.user_id)
        .join(Course, Course.id == Enrollment.course_id)
        .where(*conditions)
        .order_by(Enrollment.id)
    ).all()


def _course_grant(row, settings: PlatformSettings, *, by_rules: bool) -> _Grant | None:
    """The certificate of an enrollment's row; by the rules (issued by itself): None unless it qualifies."""
    user_id, course_id, completed_at, name, title, course_settings, seconds, modules, total, count = row
    facts = _CourseFacts(title, float(seconds or 0), int(modules or 0), completed_at, float(total or 0),
                         int(count or 0))
    details, average = _snapshot(settings, [facts])
    if by_rules:
        # The course's own choice (True or False) wins over the platform's; None follows it.
        chosen = (course_settings or {}).get("certificate")
        rules = settings.certificates
        if chosen is False or (chosen is not True and not rules.enabled):
            return None
        if rules.min_score is not None and average is not None and average < rules.min_score:
            return None
    return _Grant("course", user_id, course_id, None, name, title, details, average)


def _path_rows(db: Session, *conditions):
    """Per path assignment meeting `conditions` and published course of its path, in order: who, the path and the
    facts of its snapshot. One read."""
    seconds, modules = _course_columns()
    total, count = _score_columns()
    return db.execute(
        select(
            PathAssignment.id, PathAssignment.user_id, PathAssignment.path_id, User.name, LearningPath.title,
            LearningPath.settings, Course.title, seconds, modules, Enrollment.completed_at, total, count,
        )
        .join(User, User.id == PathAssignment.user_id)
        .join(LearningPath, LearningPath.id == PathAssignment.path_id)
        .join(LearningPathCourse, LearningPathCourse.path_id == PathAssignment.path_id)
        .join(Course, and_(Course.id == LearningPathCourse.course_id, Course.status == "published"))
        .outerjoin(Enrollment, and_(Enrollment.user_id == PathAssignment.user_id, Enrollment.course_id == Course.id))
        .where(*conditions)
        .order_by(PathAssignment.id, LearningPathCourse.order, LearningPathCourse.id)
    ).all()


def _gives_certificate(path_settings: dict | None) -> bool:
    try:
        return PathSettings.model_validate(path_settings or {}).certificate
    except ValidationError:  # edited by hand into something invalid: the defaults, as the path itself reads them
        return PathSettings().certificate


def _path_grants(rows, settings: PlatformSettings, *, by_rules: bool) -> list[_Grant]:
    by_assignment: dict[int, list] = {}
    for row in rows:
        by_assignment.setdefault(row[0], []).append(row)
    grants = []
    for path_rows in by_assignment.values():
        _, user_id, path_id, name, title, path_settings = path_rows[0][:6]
        if by_rules and not (settings.certificates.enabled and _gives_certificate(path_settings)):
            continue
        courses = [
            _CourseFacts(course_title, float(seconds or 0), int(modules or 0), completed_at, float(total or 0),
                         int(count or 0))
            for *_, course_title, seconds, modules, completed_at, total, count in path_rows
        ]
        details, average = _snapshot(settings, courses)
        grants.append(_Grant("path", user_id, None, path_id, name, title, details, average))
    return grants


def _issued(kind: str):
    """Correlated: the person already has a certificate (in force or not) of this course or path, as the enrollment
    or path assignment of the outer query says."""
    if kind == "course":
        target = and_(Certificate.user_id == Enrollment.user_id, Certificate.course_id == Enrollment.course_id)
        return exists().where(Certificate.kind == "course", target).correlate(Enrollment)
    target = and_(Certificate.user_id == PathAssignment.user_id, Certificate.path_id == PathAssignment.path_id)
    return exists().where(Certificate.kind == "path", target).correlate(PathAssignment)


# ── Writing ───────────────────────────────────────────────────────────


def _values(grant: _Grant, settings: PlatformSettings, now: datetime, code: str) -> dict[str, Any]:
    validity = settings.certificates.validity_months
    return {
        "code": code,
        "kind": grant.kind,
        "user_id": grant.user_id,
        "course_id": grant.course_id,
        "path_id": grant.path_id,
        "recipient_name": grant.recipient_name[:200],
        "title": grant.title[:300],
        "details": grant.details,
        "score": grant.score,
        "issued_at": now,
        "expires_at": add_months(now, validity) if validity else None,
    }


def _write(db: Session, grants: list[_Grant], settings: PlatformSettings) -> int:
    """Insert in bulk; returns how many were written. One that exists already (issued meanwhile by another request)
    is skipped, and so would be the one in a trillion whose new code is taken: the next maintenance pass issues it."""
    if not grants:
        return 0
    now, codes = utcnow(), set()
    while len(codes) < len(grants):
        codes.add(new_code())
    rows = [_values(grant, settings, now, code) for grant, code in zip(grants, codes)]
    insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
    written = 0
    for start in range(0, len(rows), _BATCH):
        result = db.execute(insert(Certificate).values(rows[start:start + _BATCH]).on_conflict_do_nothing())
        written += max(result.rowcount or 0, 0)
    return written


def issue_for_enrollments(db: Session, enrollment_ids: Iterable[int]) -> int:
    """Enrollments that just became completed: their courses' certificates, for whoever has none yet and qualifies."""
    enrollment_ids = list(enrollment_ids)
    if not enrollment_ids:
        return 0
    settings = platform.get_settings(db)
    db.flush()  # their completion is read below
    rows = _course_rows(db, Enrollment.id.in_(enrollment_ids), Enrollment.status == "completed", ~_issued("course"))
    return _write(db, [grant for row in rows if (grant := _course_grant(row, settings, by_rules=True))], settings)


def issue_for_assignments(db: Session, assignment_ids: Iterable[int]) -> int:
    """Path assignments that just became completed: their paths' certificates, for whoever has none yet."""
    assignment_ids = list(assignment_ids)
    if not assignment_ids:
        return 0
    settings = platform.get_settings(db)
    rows = _path_rows(db, PathAssignment.id.in_(assignment_ids), PathAssignment.completed_at.isnot(None),
                      ~_issued("path"))
    return _write(db, _path_grants(rows, settings, by_rules=True), settings)


def backfill(db: Session) -> int:
    """The certificates missing for what was completed before they existed (or while they were off), by the same
    rules. Idempotent: whoever has one of that course or path, in force or not, is skipped."""
    settings = platform.get_settings(db)
    rows = _course_rows(db, Enrollment.status == "completed", ~_issued("course"))
    grants = [grant for row in rows if (grant := _course_grant(row, settings, by_rules=True))]
    rows = _path_rows(db, PathAssignment.completed_at.isnot(None), ~_issued("path"))
    written = _write(db, grants + _path_grants(rows, settings, by_rules=True), settings)
    db.commit()
    return written


# ── By hand ───────────────────────────────────────────────────────────


def in_force(
    db: Session, user_id: int, *, course_id: int | None = None, path_id: int | None = None
) -> Certificate | None:
    """The person's certificate of this course (or path) that is not revoked: valid or expired, at most one."""
    target = Certificate.course_id == course_id if course_id is not None else Certificate.path_id == path_id
    kind = "course" if course_id is not None else "path"
    return (
        db.query(Certificate)
        .filter(Certificate.user_id == user_id, Certificate.kind == kind, target, Certificate.revoked_at.is_(None))
        .first()
    )


def _create(db: Session, grant: _Grant, settings: PlatformSettings) -> tuple[Certificate, bool]:
    """One certificate; True when it is new. Another one in force written meanwhile (by the person completing it, by
    another admin) comes back instead; a code already taken gets another one."""
    for _ in range(5):
        certificate = Certificate(**_values(grant, settings, utcnow(), new_code()))
        try:
            with db.begin_nested():
                db.add(certificate)
        except IntegrityError:
            current = in_force(db, grant.user_id, course_id=grant.course_id, path_id=grant.path_id)
            if current is not None:
                return current, False
            continue
        return certificate, True
    raise RuntimeError("certificate code space exhausted")  # five codes taken in a row: never in practice


def issue_course(db: Session, user_id: int, course_id: int) -> tuple[Certificate, bool]:
    """By hand, for a course the person completed (the platform's rules don't apply). The one they already have in
    force comes back instead of a second one; True when it is new."""
    current = in_force(db, user_id, course_id=course_id)
    if current is not None:
        return current, False
    rows = _course_rows(db, Enrollment.user_id == user_id, Enrollment.course_id == course_id,
                        Enrollment.status == "completed")
    if not rows:
        raise CertificateError("La persona aún no completa el curso.")
    settings = platform.get_settings(db)
    return _create(db, _course_grant(rows[0], settings, by_rules=False), settings)


def issue_path(db: Session, assignment: PathAssignment) -> tuple[Certificate, bool]:
    """By hand, for a path assignment the caller checked is completed (as `issue_course`)."""
    current = in_force(db, assignment.user_id, path_id=assignment.path_id)
    if current is not None:
        return current, False
    settings = platform.get_settings(db)
    grants = _path_grants(_path_rows(db, PathAssignment.id == assignment.id), settings, by_rules=False)
    if not grants:
        raise CertificateError("La ruta no tiene cursos publicados.")
    return _create(db, grants[0], settings)


def forget(db: Session, *, course_id: int | None = None, path_id: int | None = None) -> None:
    """A course (or path) is being deleted: its certificates stay, with their snapshot, linked to nothing. The
    database's foreign key does the same; SQLite (development) doesn't enforce it, and could give its id to another."""
    column = Certificate.course_id if course_id is not None else Certificate.path_id
    db.execute(
        update(Certificate).where(column == (course_id if course_id is not None else path_id)).values({column: None}),
        execution_options={"synchronize_session": False},
    )


def revoke(certificate: Certificate, reason: str | None = None) -> None:
    """Out of force from now on (its verification says so). Revoking it again changes nothing."""
    if certificate.revoked_at is None:
        certificate.revoked_at = utcnow()
        certificate.revoked_reason = (reason or "").strip() or None


def reissue(db: Session, certificate: Certificate) -> Certificate:
    """A new certificate in its place: a new code and a fresh snapshot (today's names, hours, score, issuer, signer
    and validity). It is revoked, if it was not already."""
    course_id, path_id = (certificate.course_id, None) if certificate.kind == "course" else (None, certificate.path_id)
    if course_id is None and path_id is None:
        what = "El curso" if certificate.kind == "course" else "La ruta"
        raise CertificateError(f"{what} de este certificado ya no existe: no se puede reemitir.")
    current = in_force(db, certificate.user_id, course_id=course_id, path_id=path_id)
    if current is not None and current.id != certificate.id:
        raise CertificateError(f"Esta persona ya tiene otro certificado vigente ({current.code}): reemite ese.")
    settings = platform.get_settings(db)
    if course_id is not None:
        rows = _course_rows(db, Enrollment.user_id == certificate.user_id, Enrollment.course_id == course_id)
        grants = [_course_grant(row, settings, by_rules=False) for row in rows]
        missing = "La persona ya no está inscrita en el curso: no se puede reemitir."
    else:
        rows = _path_rows(db, PathAssignment.user_id == certificate.user_id, PathAssignment.path_id == path_id)
        grants = _path_grants(rows, settings, by_rules=False)
        missing = "La persona ya no tiene la ruta (o no tiene cursos publicados): no se puede reemitir."
    if not grants:
        raise CertificateError(missing)
    was_in_force = certificate.revoked_at is None
    revoke(certificate)
    db.flush()  # out of force before the new one takes its place
    new, _ = _create(db, grants[0], settings)
    if was_in_force:
        certificate.revoked_reason = f"Reemitido como {new.code}"
    return new
