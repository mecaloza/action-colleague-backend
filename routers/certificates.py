from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import Certificate, Enrollment
from schemas import CertificateCreate, CertificateOut

router = APIRouter(prefix="/certificates", tags=["certificates"])


@router.get("/", response_model=List[CertificateOut])
def list_certificates(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return db.query(Certificate).offset(skip).limit(limit).all()


@router.get("/{certificate_id}", response_model=CertificateOut)
def get_certificate(certificate_id: int, db: Session = Depends(get_db)):
    cert = db.get(Certificate, certificate_id)
    if not cert:
        raise HTTPException(404, "Certificate not found")
    return cert


@router.post("/", response_model=CertificateOut, status_code=201)
def create_certificate(payload: CertificateCreate, db: Session = Depends(get_db)):
    enrollment = db.get(Enrollment, payload.enrollment_id)
    if not enrollment:
        raise HTTPException(404, "Enrollment not found")
    if enrollment.status != "completed":
        raise HTTPException(400, "Enrollment not completed yet")
    existing = db.query(Certificate).filter(Certificate.enrollment_id == payload.enrollment_id).first()
    if existing:
        raise HTTPException(400, "Certificate already exists for this enrollment")
    cert = Certificate(**payload.model_dump())
    db.add(cert)
    db.commit()
    db.refresh(cert)
    return cert
