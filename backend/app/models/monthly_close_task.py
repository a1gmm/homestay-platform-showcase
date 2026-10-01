"""Durable administrator goals; financial authority remains in proposal services."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MonthlyCloseTask(Base):
    __tablename__ = "monthly_close_tasks"
    __table_args__ = (
        UniqueConstraint("cycle_id", "actor_id", name="uq_monthly_close_tasks_cycle_actor"),
        CheckConstraint("status IN ('queued','running','waiting_user','waiting_approval','paused','succeeded','failed')", name="ck_monthly_close_tasks_status"),
        Index("ix_monthly_close_tasks_dispatch", "status", "next_run_at"),
    )

    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(24), ForeignKey("monthly_close_cycles.cycle_id"), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(20), ForeignKey("users.user_id"), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    lease_token: Mapped[str | None] = mapped_column(String(32))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    events: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    last_safe_error: Mapped[str | None] = mapped_column(String(250))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
