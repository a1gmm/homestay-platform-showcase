from sqlalchemy import String, Boolean, Enum as PgEnum, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
import enum

from app.core.database import Base


class UserRole(str, enum.Enum):
    admin = "admin"
    operator = "operator"
    finance = "finance"
    cleaner = "cleaner"
    owner = "owner"
    keeper = "keeper"  # 管家


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(50), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[UserRole] = mapped_column(PgEnum(UserRole, name="user_role"), nullable=False, default=UserRole.operator)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    phone: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    orders = relationship("Order", foreign_keys="Order.created_by", back_populates="creator")
    audit_logs = relationship("AuditLog", back_populates="operator")
