import unicodedata
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_user, require_admin
from database import get_db
from models import Evaluation, Module, ModuleProgress, Enrollment, User
from schemas import (
    EvaluationCreate, EvaluationOut, EvaluationUpdate,
    EvaluationSubmit, EvaluationResult, ModuleProgressOut,
)

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


def _to_out(ev: Evaluation) -> dict:
    return {
        "id": ev.id,
        "module_id": ev.module_id,
        "questions": ev.questions,
        "created_at": ev.created_at,
    }


@router.get("/", response_model=List[EvaluationOut])
def list_evaluations(
    module_id: Optional[int] = None, skip: int = 0, limit: int = 100, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
):
    q = db.query(Evaluation)
    if module_id is not None:
        q = q.filter(Evaluation.module_id == module_id)
    return [_to_out(e) for e in q.offset(skip).limit(limit).all()]


@router.get("/{evaluation_id}", response_model=EvaluationOut)
def get_evaluation(evaluation_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    ev = db.get(Evaluation, evaluation_id)
    if not ev:
        raise HTTPException(404, "Evaluation not found")
    return _to_out(ev)


@router.post("/", response_model=EvaluationOut, status_code=201)
def create_evaluation(payload: EvaluationCreate, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    ev = Evaluation(module_id=payload.module_id)
    ev.questions = payload.questions
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return _to_out(ev)


@router.patch("/{evaluation_id}", response_model=EvaluationOut)
def update_evaluation(
    evaluation_id: int, payload: EvaluationUpdate, db: Session = Depends(get_db), _admin: User = Depends(require_admin)
):
    ev = db.get(Evaluation, evaluation_id)
    if not ev:
        raise HTTPException(404, "Evaluation not found")
    if payload.questions is not None:
        ev.questions = payload.questions
    db.commit()
    db.refresh(ev)
    return _to_out(ev)


@router.delete("/{evaluation_id}", status_code=204)
def delete_evaluation(evaluation_id: int, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    ev = db.get(Evaluation, evaluation_id)
    if not ev:
        raise HTTPException(404, "Evaluation not found")
    db.delete(ev)
    db.commit()


PASSING_SCORE = 70.0  # Minimum score to pass (%)


@router.post("/submit", response_model=EvaluationResult)
def submit_evaluation(
    payload: EvaluationSubmit,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Submit answers for a module evaluation. Calculates score and updates progress."""
    # Validate enrollment
    enrollment = db.get(Enrollment, payload.enrollment_id)
    if not enrollment:
        raise HTTPException(404, "Enrollment not found")

    # Validate module
    module = db.get(Module, payload.module_id)
    if not module:
        raise HTTPException(404, "Module not found")

    # Check sequential access: previous module must be passed
    if module.order > 1:
        prev_module = db.query(Module).filter(
            Module.course_id == module.course_id,
            Module.order == module.order - 1,
        ).first()
        if prev_module:
            prev_progress = db.query(ModuleProgress).filter(
                ModuleProgress.enrollment_id == payload.enrollment_id,
                ModuleProgress.module_id == prev_module.id,
                ModuleProgress.passed == True,
            ).first()
            if not prev_progress:
                raise HTTPException(403, f"Debes aprobar el módulo anterior primero (Módulo {module.order - 1})")

    # Get evaluation
    evaluation = db.query(Evaluation).filter(Evaluation.module_id == payload.module_id).first()
    if not evaluation:
        raise HTTPException(404, "No hay evaluación para este módulo")

    questions = evaluation.questions
    if not questions:
        raise HTTPException(400, "La evaluación no tiene preguntas")

    # Calculate score — supports all question types
    correct = 0
    total = len(questions)
    for answer in payload.answers:
        if not isinstance(answer, dict):
            continue
        q_idx = answer.get("question_index", -1)
        if not (0 <= q_idx < total):
            continue
        q = questions[q_idx]
        q_type = q.get("type", "multiple_choice")

        if q_type in ("scenario", "multiple_choice"):
            # Index-based or letter-based match
            selected = answer.get("selected", "")
            correct_answer = q.get("correct", "")
            if str(selected).strip().lower() == str(correct_answer).strip().lower():
                correct += 1

        elif q_type == "ordering":
            submitted_order = answer.get("selected", [])
            correct_order = q.get("correct_order", [])
            if isinstance(submitted_order, list) and submitted_order == correct_order:
                correct += 1

        elif q_type == "matching":
            submitted_pairs = answer.get("selected", [])
            expected_pairs = q.get("pairs", [])
            if isinstance(submitted_pairs, list) and len(submitted_pairs) == len(expected_pairs):
                all_match = all(
                    isinstance(sp, dict)
                    and sp.get("left", "").strip() == ep.get("left", "").strip()
                    and sp.get("right", "").strip() == ep.get("right", "").strip()
                    for sp, ep in zip(submitted_pairs, expected_pairs)
                )
                if all_match:
                    correct += 1

        elif q_type == "fill_blank":
            submitted_text = str(answer.get("selected", "")).strip()
            correct_text = str(q.get("answer", "")).strip()
            # Normalize: lowercase, strip accents
            def _normalize(s: str) -> str:
                s = s.lower().strip()
                return "".join(
                    c for c in unicodedata.normalize("NFD", s)
                    if unicodedata.category(c) != "Mn"
                )
            if _normalize(submitted_text) == _normalize(correct_text):
                correct += 1

        elif q_type == "true_false":
            submitted_val = answer.get("selected")
            correct_val = q.get("correct")
            # Handle both bool and string representations
            def _to_bool(v) -> Optional[bool]:
                if isinstance(v, bool):
                    return v
                if isinstance(v, str):
                    return v.strip().lower() in ("true", "1", "verdadero")
                return None
            if _to_bool(submitted_val) is not None and _to_bool(submitted_val) == _to_bool(correct_val):
                correct += 1

        else:
            # Legacy format fallback
            selected = answer.get("selected", "")
            correct_answer = q.get("correct", q.get("answer", ""))
            if str(selected).strip().lower() == str(correct_answer).strip().lower():
                correct += 1

    score = (correct / total * 100) if total > 0 else 0
    passed = score >= PASSING_SCORE

    # Get or create progress
    progress = db.query(ModuleProgress).filter(
        ModuleProgress.enrollment_id == payload.enrollment_id,
        ModuleProgress.module_id == payload.module_id,
    ).first()

    if not progress:
        progress = ModuleProgress(
            enrollment_id=payload.enrollment_id,
            module_id=payload.module_id,
        )
        db.add(progress)

    progress.score = score
    progress.passed = passed
    progress.attempts = (progress.attempts or 0) + 1
    if passed:
        progress.completed = True
        progress.completed_at = datetime.utcnow()

    db.commit()
    db.refresh(progress)

    # Check if next module is unlocked
    next_module_unlocked = False
    if passed:
        next_mod = db.query(Module).filter(
            Module.course_id == module.course_id,
            Module.order == module.order + 1,
        ).first()
        next_module_unlocked = next_mod is not None

    return EvaluationResult(
        module_id=payload.module_id,
        enrollment_id=payload.enrollment_id,
        score=round(score, 1),
        passed=passed,
        correct=correct,
        total=total,
        attempts=progress.attempts,
        next_module_unlocked=next_module_unlocked,
    )


@router.get("/progress/{enrollment_id}", response_model=List[ModuleProgressOut])
def get_course_progress(
    enrollment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get all module progress for an enrollment."""
    progress = db.query(ModuleProgress).filter(
        ModuleProgress.enrollment_id == enrollment_id,
    ).all()
    return progress


@router.get("/can-access/{enrollment_id}/{module_id}")
def can_access_module(
    enrollment_id: int,
    module_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Check if user can access a specific module (previous module passed)."""
    module = db.get(Module, module_id)
    if not module:
        raise HTTPException(404, "Module not found")

    # First module is always accessible
    if module.order <= 1:
        return {"can_access": True, "reason": "Primer módulo"}

    # Check previous module
    prev_module = db.query(Module).filter(
        Module.course_id == module.course_id,
        Module.order == module.order - 1,
    ).first()

    if not prev_module:
        return {"can_access": True, "reason": "No hay módulo previo"}

    prev_progress = db.query(ModuleProgress).filter(
        ModuleProgress.enrollment_id == enrollment_id,
        ModuleProgress.module_id == prev_module.id,
        ModuleProgress.passed == True,
    ).first()

    if prev_progress:
        return {"can_access": True, "reason": "Módulo anterior aprobado"}

    return {
        "can_access": False,
        "reason": f"Debes aprobar el Módulo {prev_module.order} primero",
        "required_module_id": prev_module.id,
    }
