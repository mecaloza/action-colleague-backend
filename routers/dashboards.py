from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import Certificate, Course, Enrollment, User
from schemas import AdminDashboard, CollaboratorDashboard, EnrollmentOut

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


@router.get("/admin", response_model=AdminDashboard)
def admin_dashboard(db: Session = Depends(get_db)):
    total_users = db.query(User).count()
    total_courses = db.query(Course).count()
    total_enrollments = db.query(Enrollment).count()
    completed = db.query(Enrollment).filter(Enrollment.status == "completed").count()
    active = db.query(Enrollment).filter(Enrollment.status == "in_progress").count()
    total_certs = db.query(Certificate).count()
    return AdminDashboard(
        total_users=total_users,
        total_courses=total_courses,
        total_enrollments=total_enrollments,
        completed_enrollments=completed,
        active_enrollments=active,
        total_certificates=total_certs,
    )


@router.get("/collaborator/{user_id}", response_model=CollaboratorDashboard)
def collaborator_dashboard(user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    enrollments = db.query(Enrollment).filter(Enrollment.user_id == user_id).all()
    completed = sum(1 for e in enrollments if e.status == "completed")
    in_progress = sum(1 for e in enrollments if e.status == "in_progress")
    certs = db.query(Certificate).join(Enrollment).filter(Enrollment.user_id == user_id).count()

    return CollaboratorDashboard(
        user_id=user_id,
        enrollments=[EnrollmentOut.model_validate(e) for e in enrollments],
        completed_courses=completed,
        in_progress_courses=in_progress,
        certificates=certs,
    )
