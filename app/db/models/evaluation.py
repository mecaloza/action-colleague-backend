import json
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONList, updated_at_column


class Evaluation(Base):
    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id"), unique=True)
    # Canonical (v2) questions. `questions_json` keeps the previous app's format until migrated.
    spec: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONList)
    questions_json: Mapped[str | None] = mapped_column(Text, default="[]")
    max_attempts: Mapped[int | None] = mapped_column(default=3, server_default="3")
    passing_score: Mapped[int] = mapped_column(default=70, server_default="70")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = updated_at_column()

    module = relationship("Module", back_populates="evaluation")
    attempts = relationship("EvaluationAttempt", back_populates="evaluation", cascade="all, delete-orphan")

    @property
    def questions(self) -> list:
        return json.loads(self.questions_json) if self.questions_json else []

    @questions.setter
    def questions(self, value: list):
        self.questions_json = json.dumps(value)


class EvaluationAttempt(Base):
    __tablename__ = "evaluation_attempts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"))
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id"))
    answers_json: Mapped[str | None] = mapped_column(Text, default="[]")
    # Per-question grading of the attempt (canonical format).
    results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONList)
    score: Mapped[float | None]
    passed: Mapped[bool | None] = mapped_column(default=False)
    attempt_number: Mapped[int | None] = mapped_column(default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

    evaluation = relationship("Evaluation", back_populates="attempts")
    user = relationship("User")
    enrollment = relationship("Enrollment", back_populates="attempts")
    module = relationship("Module")
