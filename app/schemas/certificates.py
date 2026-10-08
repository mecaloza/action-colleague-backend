from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime

CertificateKind = Literal["course", "path"]
CertificateStatus = Literal["valid", "expired", "revoked"]


class CertificateRef(BaseModel):
    """A learner's certificate, as the course and path views link it."""

    id: int
    code: str
    pdf_url: str  # absolute: opens without a session
    verify_url: str  # the web app's public verification page


class CertificateCourse(BaseModel):
    title: str
    completed_at: UtcDatetime | None = None


class CertificateDetails(BaseModel):
    """The snapshot taken when it was issued."""

    hours: float = 0  # of the course's (or path's) videos
    modules: int = 0
    courses: list[CertificateCourse] = []  # the course, or the path's published courses in order
    average_score: float | None = None  # of the best quiz scores (None: no quiz taken)
    company_name: str = ""
    signer_name: str = ""
    signer_title: str = ""
    timezone: str | None = None  # where its dates are written: the platform's then (None: the platform's now)


class LearnerCertificate(BaseModel):
    id: int
    code: str
    kind: CertificateKind
    title: str
    issued_at: UtcDatetime
    expires_at: UtcDatetime | None = None
    status: CertificateStatus
    course_id: int | None = None  # null once the course is deleted (the certificate stays)
    path_id: int | None = None
    score: float | None = None
    pdf_url: str
    verify_url: str


class AdminCertificate(LearnerCertificate):
    recipient_name: str
    user_id: int
    revoked_at: UtcDatetime | None = None
    revoked_reason: str | None = None


class AdminCertificatePage(BaseModel):
    items: list[AdminCertificate]
    total: int


class PublicCertificate(BaseModel):
    """What anyone with the code sees: enough to check it is genuine and in force."""

    code: str
    status: CertificateStatus
    kind: CertificateKind
    recipient_name: str
    title: str
    issued_at: UtcDatetime
    expires_at: UtcDatetime | None = None
    issuer_name: str
    details: CertificateDetails
    pdf_url: str


class CertificateIssue(BaseModel):
    """By hand, for a course or a path (one of them) the person completed."""

    user_id: int
    course_id: int | None = None
    path_id: int | None = None


class CertificateRevoke(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
