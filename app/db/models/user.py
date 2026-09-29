import json

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import relationship

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    email = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(300), nullable=False, default="")
    role = Column(String(20), nullable=False, default="collaborator")  # admin | collaborator
    position = Column(String(200), default="")
    department = Column(String(200), default="")
    preferred_language = Column(String(5), default="es")
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


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token = Column(String(300), unique=True, nullable=False, index=True)  # sha256 of the token
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User")
