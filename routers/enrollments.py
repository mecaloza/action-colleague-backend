from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_user, require_admin
from database import get_db
from models import Enrollment, User, Course, Module, ModuleProgress
from schemas import EnrollmentCreate, EnrollmentOut, EnrollmentUpdate

router = APIRouter(prefix="/enrollments", tags=["enrollments"])


@router.get("/", response_model=List[EnrollmentOut])
def list_enrollments(
    user_id: Optional[int] = None,
    course_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    q = db.query(Enrollment)
    if user_id is not None:
        q = q.filter(Enrollment.user_id == user_id)
    if course_id is not None:
        q = q.filter(Enrollment.course_id == course_id)
    return q.offset(skip).limit(limit).all()


@router.get("/{enrollment_id}", response_model=EnrollmentOut)
def get_enrollment(enrollment_id: int, db: Session = Depends(get_db)):
    enrollment = db.get(Enrollment, enrollment_id)
    if not enrollment:
        raise HTTPException(404, "Enrollment not found")
    return enrollment


@router.post("/", response_model=EnrollmentOut, status_code=201)
def create_enrollment(payload: EnrollmentCreate, db: Session = Depends(get_db)):
    existing = (
        db.query(Enrollment)
        .filter(Enrollment.user_id == payload.user_id, Enrollment.course_id == payload.course_id)
        .first()
    )
    if existing:
        raise HTTPException(400, "User already enrolled in this course")
    enrollment = Enrollment(**payload.model_dump())
    db.add(enrollment)
    db.commit()
    db.refresh(enrollment)
    return enrollment


@router.patch("/{enrollment_id}", response_model=EnrollmentOut)
def update_enrollment(
    enrollment_id: int, payload: EnrollmentUpdate, db: Session = Depends(get_db)
):
    enrollment = db.get(Enrollment, enrollment_id)
    if not enrollment:
        raise HTTPException(404, "Enrollment not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(enrollment, k, v)
    db.commit()
    db.refresh(enrollment)
    return enrollment


@router.delete("/{enrollment_id}", status_code=204)
def delete_enrollment(enrollment_id: int, db: Session = Depends(get_db)):
    enrollment = db.get(Enrollment, enrollment_id)
    if not enrollment:
        raise HTTPException(404, "Enrollment not found")
    db.delete(enrollment)
    db.commit()


# ── Collaborator endpoints ────────────────────────────────────────────────────

@router.get("/my-courses/list")
def my_courses(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get all courses the current user is enrolled in, with progress."""
    enrollments = db.query(Enrollment).filter(Enrollment.user_id == user.id).all()
    results = []
    for enr in enrollments:
        course = db.get(Course, enr.course_id)
        if not course:
            continue
        # Calculate progress
        modules = db.query(Module).filter(Module.course_id == course.id).all()
        total_modules = len(modules)
        completed_modules = 0
        if total_modules > 0:
            module_ids = [m.id for m in modules]
            completed_modules = db.query(ModuleProgress).filter(
                ModuleProgress.enrollment_id == enr.id,
                ModuleProgress.module_id.in_(module_ids),
                ModuleProgress.passed == True,
            ).count()
        progress_pct = (completed_modules / total_modules * 100) if total_modules > 0 else 0
        # Update progress in enrollment
        enr.progress_pct = progress_pct
        if progress_pct >= 100:
            enr.status = "completed"
        elif progress_pct > 0:
            enr.status = "in_progress"
        db.commit()

        results.append({
            "enrollment_id": enr.id,
            "course_id": course.id,
            "course_title": course.title,
            "course_description": course.description,
            "status": enr.status,
            "progress_pct": round(progress_pct, 1),
            "total_modules": total_modules,
            "completed_modules": completed_modules,
            "enrolled_at": enr.enrolled_at,
        })
    return results


@router.post("/enroll")
def enroll_self(
    course_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Self-enroll current user in a course."""
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Curso no encontrado")
    existing = db.query(Enrollment).filter(
        Enrollment.user_id == user.id,
        Enrollment.course_id == course_id,
    ).first()
    if existing:
        return {"message": "Ya estás inscrito", "enrollment_id": existing.id}
    enrollment = Enrollment(user_id=user.id, course_id=course_id, status="assigned")
    db.add(enrollment)
    db.commit()
    db.refresh(enrollment)
    return {"message": "Inscripción exitosa", "enrollment_id": enrollment.id}


@router.get("/my-course/{course_id}")
def my_course_detail(
    course_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get detailed course view for collaborator with module access status."""
    enrollment = db.query(Enrollment).filter(
        Enrollment.user_id == user.id,
        Enrollment.course_id == course_id,
    ).first()
    if not enrollment:
        raise HTTPException(404, "No estás inscrito en este curso")

    course = db.get(Course, course_id)
    modules = db.query(Module).filter(Module.course_id == course_id).order_by(Module.order).all()

    module_details = []
    for mod in modules:
        progress = db.query(ModuleProgress).filter(
            ModuleProgress.enrollment_id == enrollment.id,
            ModuleProgress.module_id == mod.id,
        ).first()

        # Check if accessible (first module always, others need prev passed)
        can_access = True
        if mod.order > 1:
            prev_mod = db.query(Module).filter(
                Module.course_id == course_id,
                Module.order == mod.order - 1,
            ).first()
            if prev_mod:
                prev_progress = db.query(ModuleProgress).filter(
                    ModuleProgress.enrollment_id == enrollment.id,
                    ModuleProgress.module_id == prev_mod.id,
                    ModuleProgress.passed == True,
                ).first()
                can_access = prev_progress is not None

        module_details.append({
            "id": mod.id,
            "title": mod.title,
            "order": mod.order,
            "content_text": mod.content_text if can_access else None,
            "video_url": mod.video_url if can_access else None,
            "audio_url": mod.audio_url if can_access else None,
            "can_access": can_access,
            "passed": progress.passed if progress else False,
            "score": progress.score if progress else None,
            "attempts": progress.attempts if progress else 0,
            "completed_at": progress.completed_at if progress else None,
        })

    total = len(modules)
    completed = sum(1 for m in module_details if m["passed"])

    return {
        "enrollment_id": enrollment.id,
        "course": {
            "id": course.id,
            "title": course.title,
            "description": course.description,
        },
        "progress_pct": round((completed / total * 100) if total > 0 else 0, 1),
        "total_modules": total,
        "completed_modules": completed,
        "modules": module_details,
    }
