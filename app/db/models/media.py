from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import relationship

from app.db.base import Base


class UserVideo(Base):
    __tablename__ = "user_videos"

    id = Column(String(36), primary_key=True, index=True)  # UUID
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    storage_url = Column(String(500), nullable=False)
    duration = Column(Integer, nullable=True)  # seconds
    file_size = Column(Integer, nullable=True)  # bytes
    format = Column(String(20), default="webm")  # webm, mp4
    created_at = Column(DateTime, server_default=func.now())
    status = Column(String(20), default="uploaded")  # uploaded, processing, ready

    module = relationship("Module")
    user = relationship("User")
