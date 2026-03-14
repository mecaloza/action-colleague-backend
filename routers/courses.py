from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import get_current_user, require_admin
from database import get_db
from models import Course, Enrollment, Evaluation, Module, User
from schemas import (
    CourseCreate,
    CourseGenerateRequest,
    CourseGenerateResponse,
    CourseOut,
    CourseUpdate,
)

router = APIRouter(prefix="/courses", tags=["courses"])


@router.get("/", response_model=List[CourseOut])
def list_courses(
    skip: int = 0,
    limit: int = 100,
    language: Optional[str] = None,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    query = db.query(Course)
    if language is not None:
        if language not in {"es", "en", "pt"}:
            raise HTTPException(400, "language must be one of: es, en, pt")
        query = query.filter(Course.language == language)
    return query.offset(skip).limit(limit).all()


@router.get("/{course_id}", response_model=CourseOut)
def get_course(course_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Course not found")
    return course


@router.get("/{course_id}/stats")
def get_course_stats(course_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Course not found")

    total_modules = db.query(func.count(Module.id)).filter(Module.course_id == course_id).scalar()

    total_evaluations = (
        db.query(func.count(Evaluation.id))
        .join(Module, Evaluation.module_id == Module.id)
        .filter(Module.course_id == course_id)
        .scalar()
    )

    total_enrollments = db.query(func.count(Enrollment.id)).filter(Enrollment.course_id == course_id).scalar()

    completed = (
        db.query(func.count(Enrollment.id))
        .filter(Enrollment.course_id == course_id, Enrollment.status == "completed")
        .scalar()
    )

    completion_rate = (completed / total_enrollments * 100) if total_enrollments > 0 else 0.0

    return {
        "total_modules": total_modules,
        "total_evaluations": total_evaluations,
        "total_enrollments": total_enrollments,
        "completion_rate": round(completion_rate, 2),
    }


@router.post("/", response_model=CourseOut, status_code=201)
def create_course(payload: CourseCreate, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    course = Course(**payload.model_dump())
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


@router.patch("/{course_id}", response_model=CourseOut)
def update_course(course_id: int, payload: CourseUpdate, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Course not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(course, k, v)
    db.commit()
    db.refresh(course)
    return course


@router.delete("/{course_id}", status_code=204)
def delete_course(course_id: int, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Course not found")
    db.delete(course)
    db.commit()


@router.post("/{course_id}/generate", response_model=CourseGenerateResponse)
def generate_course_content(
    course_id: int, payload: CourseGenerateRequest, db: Session = Depends(get_db), _admin: User = Depends(require_admin)
):
    """AI stub: generates modules for a course."""
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Course not found")

    topic = payload.topic or course.title
    created = 0
    for i in range(1, payload.num_modules + 1):
        module = Module(
            course_id=course_id,
            title=f"{topic} - Module {i}",
            order=i,
            content_text=f"[AI-generated content placeholder for '{topic}' module {i}]",
        )
        db.add(module)
        created += 1

        evaluation = Evaluation(module=module)
        evaluation.questions = [
            {
                "question": f"Sample question {q} for {topic} module {i}",
                "options": ["A", "B", "C", "D"],
                "correct": 0,
            }
            for q in range(1, 4)
        ]
        db.add(evaluation)

    db.commit()
    return CourseGenerateResponse(
        message=f"Generated {created} modules for '{topic}' (AI stub)",
        course_id=course_id,
        modules_created=created,
    )
