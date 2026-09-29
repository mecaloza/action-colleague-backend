"""Loaders shared by routers: fetch a row or answer 404 in Spanish."""

from typing import TypeVar

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.db.models import Course, Module, User

T = TypeVar("T")


def _get_or_404(db: Session, model: type[T], row_id: int, message: str) -> T:
    row = db.get(model, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, message)
    return row


def course_or_404(db: Session, course_id: int) -> Course:
    return _get_or_404(db, Course, course_id, "Curso no encontrado")


def module_or_404(db: Session, module_id: int) -> Module:
    return _get_or_404(db, Module, module_id, "Módulo no encontrado")


def user_or_404(db: Session, user_id: int) -> User:
    return _get_or_404(db, User, user_id, "Persona no encontrada")
