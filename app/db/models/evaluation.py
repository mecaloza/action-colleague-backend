import json

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.orm import relationship

from app.db.base import Base


class Evaluation(Base):
    __tablename__ = "evaluations"

    id = Column(Integer, primary_key=True, index=True)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=False, unique=True)
    questions_json = Column(Text, default="[]")
    max_attempts = Column(Integer, default=3)
    created_at = Column(DateTime, server_default=func.now())

    module = relationship("Module", back_populates="evaluation")
    attempts = relationship("EvaluationAttempt", back_populates="evaluation")

    @property
    def questions(self) -> list:
        return json.loads(self.questions_json) if self.questions_json else []

    @questions.setter
    def questions(self, value: list):
        self.questions_json = json.dumps(value)


class EvaluationAttempt(Base):
    __tablename__ = "evaluation_attempts"

    id = Column(Integer, primary_key=True, index=True)
    evaluation_id = Column(Integer, ForeignKey("evaluations.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    enrollment_id = Column(Integer, ForeignKey("enrollments.id"), nullable=False)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=False)
    answers_json = Column(Text, default="[]")
    score = Column(Float, nullable=True)
    passed = Column(Boolean, default=False)
    attempt_number = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())

    evaluation = relationship("Evaluation", back_populates="attempts")
    user = relationship("User")
    enrollment = relationship("Enrollment")
    module = relationship("Module")
