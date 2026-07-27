from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, func
from app.database import Base


class User(Base):
    """Пользователь MES с простой ролью RBAC."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    middle_name = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    role = Column(String, nullable=False, default="manager", index=True)
    roles = Column(JSON, nullable=True)
    task_roles = Column(JSON, nullable=True)
    auto_tasks_enabled = Column(Boolean, default=True, nullable=False)
    manual_assignment_enabled = Column(Boolean, default=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
