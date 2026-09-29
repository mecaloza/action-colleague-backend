from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import Certificate, Course, Enrollment, User
from app.schemas import AdminDashboard, CollaboratorDashboard, EnrollmentOut

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


@router.get("/collaborator/{user_id}")
def collaborator_dashboard(user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    enrollments = db.query(Enrollment).filter(Enrollment.user_id == user_id).all()
    completed = sum(1 for e in enrollments if e.status == "completed")
    in_progress = sum(1 for e in enrollments if e.status == "in_progress")
    certs = db.query(Certificate).join(Enrollment).filter(Enrollment.user_id == user_id).count()

    # Build enriched enrollments with course info
    enriched = []
    total_hours = 0
    for e in enrollments:
        course = db.get(Course, e.course_id)
        course_data = None
        if course:
            duration = getattr(course, "duration_hours", 0) or 0
            total_hours += duration
            course_data = {
                "title": course.title,
                "description": getattr(course, "description", ""),
                "category": getattr(course, "category", "General"),
                "duration_hours": duration,
            }
        enriched.append({
            "id": e.id,
            "user_id": e.user_id,
            "course_id": e.course_id,
            "status": e.status,
            "progress": int(e.progress_pct) if e.progress_pct else 0,
            "enrolled_at": str(e.enrolled_at) if e.enrolled_at else None,
            "course": course_data,
        })

    return {
        "user_id": user_id,
        "enrollments": enriched,
        "completed_courses": completed,
        "in_progress_courses": in_progress,
        "certificates": certs,
        "total_hours": total_hours,
        "recent_activity": [],
    }
