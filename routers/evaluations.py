import json
import unicodedata
from collections import Counter
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_user, require_admin
from database import get_db
from models import Evaluation, EvaluationAttempt, Module, ModuleProgress, Enrollment, Course, User
from schemas import (
    EvaluationCreate, EvaluationOut, EvaluationUpdate,
    EvaluationSubmit, EvaluationResult, ModuleProgressOut,
    CourseAnalyticsResponse, ModuleAnalytics, QuestionAnalytics,
    CourseResponsesResponse, UserResponseSummary, AttemptSummary,
)

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


def _to_out(ev: Evaluation) -> dict:
    return {
        "id": ev.id,
        "module_id": ev.module_id,
        "questions": ev.questions,
        "max_attempts": ev.max_attempts or 3,
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
    ev = Evaluation(module_id=payload.module_id, max_attempts=payload.max_attempts)
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
    if payload.max_attempts is not None:
        ev.max_attempts = payload.max_attempts
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

    # Validate max_attempts
    max_attempts = evaluation.max_attempts or 3
    existing_attempts = db.query(EvaluationAttempt).filter(
        EvaluationAttempt.evaluation_id == evaluation.id,
        EvaluationAttempt.user_id == user.id,
        EvaluationAttempt.enrollment_id == payload.enrollment_id,
    ).count()

    if existing_attempts >= max_attempts:
        raise HTTPException(403, f"Has agotado los {max_attempts} intentos permitidos para esta evaluación")

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
            def _to_bool(v) -> Optional[bool]:
                if isinstance(v, bool):
                    return v
                if isinstance(v, str):
                    return v.strip().lower() in ("true", "1", "verdadero")
                return None
            if _to_bool(submitted_val) is not None and _to_bool(submitted_val) == _to_bool(correct_val):
                correct += 1

        else:
            selected = answer.get("selected", "")
            correct_answer = q.get("correct", q.get("answer", ""))
            if str(selected).strip().lower() == str(correct_answer).strip().lower():
                correct += 1

    score = (correct / total * 100) if total > 0 else 0
    passed = score >= PASSING_SCORE

    # Save attempt
    attempt_number = existing_attempts + 1
    attempt = EvaluationAttempt(
        evaluation_id=evaluation.id,
        user_id=user.id,
        enrollment_id=payload.enrollment_id,
        module_id=payload.module_id,
        answers_json=json.dumps(payload.answers, default=str),
        score=round(score, 1),
        passed=passed,
        attempt_number=attempt_number,
    )
    db.add(attempt)

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
    progress.attempts = attempt_number
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

    attempts_remaining = max_attempts - attempt_number

    return EvaluationResult(
        module_id=payload.module_id,
        enrollment_id=payload.enrollment_id,
        score=round(score, 1),
        passed=passed,
        correct=correct,
        total=total,
        attempts=attempt_number,
        attempts_remaining=max(attempts_remaining, 0),
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


# ── Analytics Endpoints ──────────────────────────────────────────────


@router.get("/analytics/{course_id}", response_model=CourseAnalyticsResponse)
def get_course_analytics(
    course_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Per-question analytics for every module in a course."""
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Course not found")

    modules = db.query(Module).filter(Module.course_id == course_id).order_by(Module.order).all()
    modules_analytics = []

    for mod in modules:
        evaluation = db.query(Evaluation).filter(Evaluation.module_id == mod.id).first()
        if not evaluation:
            continue

        questions = evaluation.questions
        if not questions:
            continue

        attempts = db.query(EvaluationAttempt).filter(
            EvaluationAttempt.evaluation_id == evaluation.id,
        ).all()

        question_stats: list[QuestionAnalytics] = []
        for q_idx, q in enumerate(questions):
            correct_count = 0
            incorrect_count = 0
            wrong_answers: list[str] = []

            for att in attempts:
                att_answers = json.loads(att.answers_json) if att.answers_json else []
                user_answer = next(
                    (a for a in att_answers if isinstance(a, dict) and a.get("question_index") == q_idx),
                    None,
                )
                if user_answer is None:
                    continue

                is_correct = _check_answer(q, user_answer)
                if is_correct:
                    correct_count += 1
                else:
                    incorrect_count += 1
                    wrong_answers.append(str(user_answer.get("selected", "")))

            total_responses = correct_count + incorrect_count
            accuracy = (correct_count / total_responses * 100) if total_responses > 0 else 0
            most_common_wrong = None
            if wrong_answers:
                most_common_wrong = Counter(wrong_answers).most_common(1)[0][0]

            question_stats.append(QuestionAnalytics(
                question_index=q_idx,
                question_text=q.get("question", q.get("text", "")),
                question_type=q.get("type", "multiple_choice"),
                total_responses=total_responses,
                correct_count=correct_count,
                incorrect_count=incorrect_count,
                accuracy_pct=round(accuracy, 1),
                most_common_wrong_answer=most_common_wrong,
            ))

        modules_analytics.append(ModuleAnalytics(
            module_id=mod.id,
            module_title=mod.title,
            questions=question_stats,
        ))

    return CourseAnalyticsResponse(course_id=course_id, modules=modules_analytics)


@router.get("/responses/{course_id}", response_model=CourseResponsesResponse)
def get_course_responses(
    course_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """List of employees with their attempts per module for a course."""
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Course not found")

    modules = db.query(Module).filter(Module.course_id == course_id).order_by(Module.order).all()
    module_ids = [m.id for m in modules]
    module_map = {m.id: m.title for m in modules}

    attempts = (
        db.query(EvaluationAttempt)
        .filter(EvaluationAttempt.module_id.in_(module_ids))
        .order_by(EvaluationAttempt.created_at)
        .all()
    )

    # Group by (user_id, module_id)
    grouped: dict[tuple[int, int], list[EvaluationAttempt]] = {}
    user_ids = set()
    for att in attempts:
        key = (att.user_id, att.module_id)
        grouped.setdefault(key, []).append(att)
        user_ids.add(att.user_id)

    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}

    responses = []
    for (uid, mid), atts in grouped.items():
        u = users.get(uid)
        responses.append(UserResponseSummary(
            user_id=uid,
            user_name=u.name if u else "Unknown",
            module_id=mid,
            module_title=module_map.get(mid, ""),
            attempts=[
                AttemptSummary(
                    score=a.score,
                    passed=a.passed,
                    answers=json.loads(a.answers_json) if a.answers_json else [],
                    created_at=a.created_at,
                )
                for a in atts
            ],
        ))

    return CourseResponsesResponse(course_id=course_id, responses=responses)


@router.get("/responses/user/{user_id}/{course_id}", response_model=CourseResponsesResponse)
def get_user_responses(
    user_id: int,
    course_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Individual user detail: attempts per module for a specific course."""
    course = db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Course not found")

    target_user = db.get(User, user_id)
    if not target_user:
        raise HTTPException(404, "User not found")

    modules = db.query(Module).filter(Module.course_id == course_id).order_by(Module.order).all()
    module_ids = [m.id for m in modules]
    module_map = {m.id: m.title for m in modules}

    attempts = (
        db.query(EvaluationAttempt)
        .filter(
            EvaluationAttempt.user_id == user_id,
            EvaluationAttempt.module_id.in_(module_ids),
        )
        .order_by(EvaluationAttempt.created_at)
        .all()
    )

    grouped: dict[int, list[EvaluationAttempt]] = {}
    for att in attempts:
        grouped.setdefault(att.module_id, []).append(att)

    responses = []
    for mid, atts in grouped.items():
        responses.append(UserResponseSummary(
            user_id=user_id,
            user_name=target_user.name,
            module_id=mid,
            module_title=module_map.get(mid, ""),
            attempts=[
                AttemptSummary(
                    score=a.score,
                    passed=a.passed,
                    answers=json.loads(a.answers_json) if a.answers_json else [],
                    created_at=a.created_at,
                )
                for a in atts
            ],
        ))

    return CourseResponsesResponse(course_id=course_id, responses=responses)


def _check_answer(question: dict, answer: dict) -> bool:
    """Check if a single answer is correct (reuses submit logic)."""
    q_type = question.get("type", "multiple_choice")

    if q_type in ("scenario", "multiple_choice"):
        selected = answer.get("selected", "")
        correct_answer = question.get("correct", "")
        return str(selected).strip().lower() == str(correct_answer).strip().lower()

    elif q_type == "ordering":
        return answer.get("selected", []) == question.get("correct_order", [])

    elif q_type == "matching":
        submitted_pairs = answer.get("selected", [])
        expected_pairs = question.get("pairs", [])
        if not isinstance(submitted_pairs, list) or len(submitted_pairs) != len(expected_pairs):
            return False
        return all(
            isinstance(sp, dict)
            and sp.get("left", "").strip() == ep.get("left", "").strip()
            and sp.get("right", "").strip() == ep.get("right", "").strip()
            for sp, ep in zip(submitted_pairs, expected_pairs)
        )

    elif q_type == "fill_blank":
        submitted = str(answer.get("selected", "")).strip()
        correct = str(question.get("answer", "")).strip()
        def _norm(s: str) -> str:
            s = s.lower().strip()
            return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
        return _norm(submitted) == _norm(correct)

    elif q_type == "true_false":
        def _to_bool(v) -> Optional[bool]:
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.strip().lower() in ("true", "1", "verdadero")
            return None
        sv = _to_bool(answer.get("selected"))
        cv = _to_bool(question.get("correct"))
        return sv is not None and sv == cv

    else:
        selected = answer.get("selected", "")
        correct_answer = question.get("correct", question.get("answer", ""))
        return str(selected).strip().lower() == str(correct_answer).strip().lower()
