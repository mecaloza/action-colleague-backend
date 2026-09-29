"""Models for features being removed from the platform (documents, series, communications)."""

import json

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from app.db.base import Base


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


class Communication(Base):
    __tablename__ = "communications"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(300), nullable=False)
    message = Column(Text, default="")
    image_url = Column(String(500), default="")
    generated = Column(Boolean, default=False)
    prompt_used = Column(Text, default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    creator = relationship("User")
