from sqlalchemy import String, Text, Enum as PgEnum, DateTime, Date, ForeignKey, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import date, datetime
import enum

from app.core.database import Base


class TaskType(str, enum.Enum):
    collect_deposit = "collect_deposit"
    cleaning = "cleaning"
    checkout_inspection = "checkout_inspection"
    return_deposit = "return_deposit"
    custom = "custom"


class TaskStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    done = "done"
    cancelled = "cancelled"


class TaskPriority(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    urgent = "urgent"


class ReviewStatus(str, enum.Enum):
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_deadline_status", "deadline", "status"),
        Index("ix_tasks_order", "order_id"),
        Index("ix_tasks_assignee_status", "assignee_id", "status"),
    )

    task_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    task_type: Mapped[TaskType] = mapped_column(PgEnum(TaskType, name="task_type"))
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    order_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("orders.order_id"))
    room_id: Mapped[str | None] = mapped_column(String(10), ForeignKey("rooms.room_id"))
    assignee_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    status: Mapped[TaskStatus] = mapped_column(PgEnum(TaskStatus, name="task_status"), default=TaskStatus.pending)
    priority: Mapped[TaskPriority] = mapped_column(PgEnum(TaskPriority, name="task_priority"), default=TaskPriority.medium)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    # Feature 4: cleaning review workflow fields
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_status: Mapped[str | None] = mapped_column(String(20))
    reviewer_id: Mapped[str | None] = mapped_column(String(20))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    # 退房打扫两步自助：谁点了【打扫卫生】(open_id) + 何时开始，供串行门闩查询。
    cleaning_started_by: Mapped[str | None] = mapped_column(String(64))
    cleaning_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    order = relationship("Order", back_populates="tasks")
