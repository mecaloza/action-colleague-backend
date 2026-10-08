"""Certificates: the admin's registry (issue by hand, revoke, reissue) and, without a session, the verification of a
code and its PDF."""

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.db.models import Certificate, Course, LearningPath, PathAssignment, User
from app.schemas.certificates import (
    AdminCertificate,
    AdminCertificatePage,
    CertificateIssue,
    CertificateRevoke,
    CertificateStatus,
    PublicCertificate,
)
from app.services import certificate_pdf, certificates, paths
from app.services import settings as platform

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/certificates", tags=["certificates"], dependencies=[Depends(require_admin)])
public_router = APIRouter(prefix="/public/certificates", tags=["certificates"])

# Drawing a PDF is CPU work in the API process, and anyone can ask for one: a few at a time, the rest wait their
# turn (or are told to try again) instead of piling up on the server's threads.
_PDF_SLOTS = threading.BoundedSemaphore(2)
PDF_WAIT_SECONDS = 10
NOT_FOUND = "Certificado no encontrado"


def _certificate(db: Session, certificate_id: int) -> Certificate:
    # Locked: two admins revoking or reissuing it at once take turns (and the second sees what the first did).
    certificate = db.query(Certificate).filter(Certificate.id == certificate_id).with_for_update().first()
    if certificate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return certificate


@router.get("", response_model=AdminCertificatePage)
def list_certificates(
    user_id: int | None = None,
    course_id: int | None = None,
    path_id: int | None = None,
    status_filter: CertificateStatus | None = Query(None, alias="status"),
    q: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Every certificate issued, newest first. `q` looks in the person (snapshot name, current name and email), the
    title and the code."""
    now = certificates.utcnow()
    query = db.query(Certificate).join(User, User.id == Certificate.user_id)
    for column, value in ((Certificate.user_id, user_id), (Certificate.course_id, course_id),
                          (Certificate.path_id, path_id)):
        if value is not None:
            query = query.filter(column == value)
    in_force = Certificate.revoked_at.is_(None)
    if status_filter == "revoked":
        query = query.filter(Certificate.revoked_at.isnot(None))
    elif status_filter == "expired":
        query = query.filter(in_force, Certificate.expires_at <= now)
    elif status_filter == "valid":
        query = query.filter(in_force, or_(Certificate.expires_at.is_(None), Certificate.expires_at > now))
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(or_(
            Certificate.recipient_name.ilike(pattern), User.name.ilike(pattern), User.email.ilike(pattern),
            Certificate.title.ilike(pattern), Certificate.code.ilike(pattern),
        ))
    total = query.count()
    rows = query.order_by(Certificate.issued_at.desc(), Certificate.id.desc()).offset(offset).limit(limit).all()
    return AdminCertificatePage(items=[certificates.admin_view(row, now) for row in rows], total=total)


def _completed_assignment(db: Session, user_id: int, path_id: int) -> PathAssignment:
    assignment = (
        db.query(PathAssignment)
        .filter(PathAssignment.user_id == user_id, PathAssignment.path_id == path_id,
                or_(PathAssignment.completed_at.isnot(None), paths.completed_clause()))
        .first()
    )
    if assignment is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "La persona aún no completa la ruta.")
    return assignment


@router.post("", response_model=AdminCertificate, status_code=status.HTTP_201_CREATED)
def issue_certificate(
    payload: CertificateIssue, response: Response, db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """By hand, for a course or a path (one of them) the person completed: the platform's rules (automatic issuing, a
    course that opts out, the minimum score) don't apply. The one they already have in force comes back (200)
    instead of a second one."""
    if (payload.course_id is None) == (payload.path_id is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Indica un curso o una ruta (solo uno de los dos).")
    if db.get(User, payload.user_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "La persona no existe.")
    try:
        if payload.course_id is not None:
            if db.get(Course, payload.course_id) is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "El curso no existe.")
            certificate, created = certificates.issue_course(db, payload.user_id, payload.course_id)
        else:
            if db.get(LearningPath, payload.path_id) is None:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "La ruta no existe.")
            assignment = _completed_assignment(db, payload.user_id, payload.path_id)
            certificate, created = certificates.issue_path(db, assignment)
    except certificates.CertificateError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    db.commit()
    if created:
        logger.info("certificate_issued_by_hand", extra={"certificate_id": certificate.id, "admin_id": admin.id})
    else:
        response.status_code = status.HTTP_200_OK
    return certificates.admin_view(certificate, certificates.utcnow())


@router.post("/{certificate_id}/revoke", response_model=AdminCertificate)
def revoke_certificate(
    certificate_id: int, payload: CertificateRevoke | None = None, db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Out of force from now on: its verification says so and its PDF is no longer served. Never issued again by
    itself; it can be reissued by hand."""
    certificate = _certificate(db, certificate_id)
    if certificate.revoked_at is None:
        certificates.revoke(certificate, payload.reason if payload else None)
        db.commit()
        logger.info("certificate_revoked", extra={"certificate_id": certificate.id, "admin_id": admin.id})
    return certificates.admin_view(certificate, certificates.utcnow())


@router.post("/{certificate_id}/reissue", response_model=AdminCertificate, status_code=status.HTTP_201_CREATED)
def reissue_certificate(certificate_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """A new certificate in its place, with a new code and today's details (names, hours, score, signer, validity);
    this one is revoked."""
    certificate = _certificate(db, certificate_id)
    try:
        new = certificates.reissue(db, certificate)
    except certificates.CertificateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    logger.info("certificate_reissued", extra={"certificate_id": certificate.id, "new_certificate_id": new.id,
                                               "admin_id": admin.id})
    return certificates.admin_view(new, certificates.utcnow())


# ── Public: verification ──────────────────────────────────────────────


def _by_code(db: Session, code: str) -> Certificate:
    normalized = certificates.normalize_code(code)
    certificate = db.query(Certificate).filter(Certificate.code == normalized).first() if normalized else None
    if certificate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return certificate


@public_router.get("/{code}", response_model=PublicCertificate)
def verify_certificate(code: str, db: Session = Depends(get_db)):
    """Whether a certificate is genuine and in force (valid, expired or revoked), and what it certifies."""
    return certificates.public_view(_by_code(db, code), certificates.utcnow())


@public_router.get("/{code}/pdf", response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
def download_certificate(code: str, db: Session = Depends(get_db)):
    """The certificate as a PDF to open, print or keep (410 once revoked)."""
    certificate = _by_code(db, code)
    if certificate.revoked_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "Este certificado fue revocado.")
    settings = platform.get_settings(db)
    # Everything is read: its connection goes back to the pool before waiting for a slot and drawing (many requests
    # at once, waiting with a connection each, would leave the rest of the API without one).
    db.close()
    if not _PDF_SLOTS.acquire(timeout=PDF_WAIT_SECONDS):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Hay muchos certificados en preparación; intenta de "
                                                               "nuevo en un momento.")
    try:
        pdf = certificate_pdf.render(certificate, settings)
    finally:
        _PDF_SLOTS.release()
    headers = {"Content-Disposition": f'inline; filename="certificado-{certificate.code}.pdf"',
               "Cache-Control": "no-store"}
    return Response(pdf, media_type="application/pdf", headers=headers)
