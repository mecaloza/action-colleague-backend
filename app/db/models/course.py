from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from app.db.base import Base


class Course(Base):
    __tablename__ = "courses"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(300), nullable=False)
    description = Column(Text, default="")
    language = Column(String(5), default="es")
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
