import json
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    email = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(300), nullable=False, default="")
    role = Column(String(20), nullable=False, default="collaborator")  # admin | collaborator
    position = Column(String(200), default="")
    department = Column(String(200), default="")
    reports_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    permissions_json = Column(Text, default="[]")
    is_active = Column(Boolean, default=True)
    hire_date = Column(Date, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    manager = relationship("User", remote_side="User.id", backref="direct_reports")
    enrollments = relationship("Enrollment", back_populates="user")
    documents = relationship("Document", back_populates="user")

    @property
    def permissions(self) -> list:
        return json.loads(self.permissions_json) if self.permissions_json else []

    @permissions.setter
    def permissions(self, value: list):
        self.permissions_json = json.dumps(value)


class Course(Base):
    __tablename__ = "courses"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(300), nullable=False)
    description = Column(Text, default="")
    status = Column(String(20), nullable=False, default="draft")  # draft | published
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    creator = relationship("User")
    modules = relationship("Module", back_populates="course", order_by="Module.order")
    enrollments = relationship("Enrollment", back_populates="course")


class Module(Base):
    __tablename__ = "modules"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    title = Column(String(300), nullable=False)
    order = Column(Integer, nullable=False, default=0)
    content_text = Column(Text, default="")
    video_url = Column(String(500), default="")
    audio_url = Column(String(500), default="")
    generation_status = Column(String(50), default="pending")  # pending, generating, completed, failed
    created_at = Column(DateTime, server_default=func.now())

    course = relationship("Course", back_populates="modules")
    evaluation = relationship("Evaluation", back_populates="module", uselist=False)
    progress_records = relationship("ModuleProgress", back_populates="module")


class Evaluation(Base):
    __tablename__ = "evaluations"

    id = Column(Integer, primary_key=True, index=True)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=False, unique=True)
    questions_json = Column(Text, default="[]")
    created_at = Column(DateTime, server_default=func.now())

    module = relationship("Module", back_populates="evaluation")

    @property
    def questions(self) -> list:
        return json.loads(self.questions_json) if self.questions_json else []

    @questions.setter
    def questions(self, value: list):
        self.questions_json = json.dumps(value)


class Enrollment(Base):
    __tablename__ = "enrollments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    status = Column(String(20), nullable=False, default="assigned")  # assigned | in_progress | completed
    progress_pct = Column(Float, default=0.0)
    enrolled_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="enrollments")
    course = relationship("Course", back_populates="enrollments")
    module_progress = relationship("ModuleProgress", back_populates="enrollment")
    certificate = relationship("Certificate", back_populates="enrollment", uselist=False)


class ModuleProgress(Base):
    __tablename__ = "module_progress"

    id = Column(Integer, primary_key=True, index=True)
    enrollment_id = Column(Integer, ForeignKey("enrollments.id"), nullable=False)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=False)
    completed = Column(Boolean, default=False)
    passed = Column(Boolean, default=False)
    score = Column(Float, nullable=True)
    attempts = Column(Integer, default=0)
    completed_at = Column(DateTime, nullable=True)

    enrollment = relationship("Enrollment", back_populates="module_progress")
    module = relationship("Module", back_populates="progress_records")


class Certificate(Base):
    __tablename__ = "certificates"

    id = Column(Integer, primary_key=True, index=True)
    enrollment_id = Column(Integer, ForeignKey("enrollments.id"), nullable=False, unique=True)
    issued_at = Column(DateTime, server_default=func.now())
    pdf_url = Column(String(500), default="")

    enrollment = relationship("Enrollment", back_populates="certificate")


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    type = Column(String(30), nullable=False)  # labor_letter | certificate
    generated_at = Column(DateTime, server_default=func.now())
    pdf_url = Column(String(500), default="")
    template_data_json = Column(Text, default="{}")

    user = relationship("User", back_populates="documents")

    @property
    def template_data(self) -> dict:
        return json.loads(self.template_data_json) if self.template_data_json else {}

    @template_data.setter
    def template_data(self, value: dict):
        self.template_data_json = json.dumps(value)


# ── Micro-Series (Sora Video Generation) ─────────────────────────────


class Series(Base):
    __tablename__ = "series"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(300), nullable=False)
    description = Column(Text, default="")
    category = Column(String(30), nullable=False, default="custom")  # caso | onboarding | compliance | custom
    thumbnail_url = Column(String(500), default="")
    status = Column(String(20), nullable=False, default="draft")  # draft | generating | published
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    creator = relationship("User")
    episodes = relationship("Episode", back_populates="series", order_by="Episode.order", cascade="all, delete-orphan")


class Episode(Base):
    __tablename__ = "episodes"

    id = Column(Integer, primary_key=True, index=True)
    series_id = Column(Integer, ForeignKey("series.id"), nullable=False)
    title = Column(String(300), nullable=False)
    synopsis = Column(Text, default="")
    order = Column(Integer, nullable=False, default=0)
    final_video_url = Column(String(500), default="")
    duration_seconds = Column(Integer, default=0)
    status = Column(String(20), nullable=False, default="draft")  # draft | generating | completed | failed
    created_at = Column(DateTime, server_default=func.now())

    series = relationship("Series", back_populates="episodes")
    scenes = relationship("Scene", back_populates="episode", order_by="Scene.order", cascade="all, delete-orphan")


class Scene(Base):
    __tablename__ = "scenes"

    id = Column(Integer, primary_key=True, index=True)
    episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=False)
    order = Column(Integer, nullable=False, default=0)
    sora_prompt = Column(Text, default="")
    narration_text = Column(Text, default="")
    video_url = Column(String(500), default="")
    audio_url = Column(String(500), default="")
    sora_video_id = Column(String(200), default="")
    duration_seconds = Column(Integer, default=8)  # 4 | 8 | 12
    status = Column(String(50), default="draft")  # draft | generating_video | generating_audio | completed | failed
    created_at = Column(DateTime, server_default=func.now())

    episode = relationship("Episode", back_populates="scenes")


# ── Refresh Tokens ────────────────────────────────────────────────────


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token = Column(String(300), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User")
