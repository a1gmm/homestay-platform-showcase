from sqlalchemy import (
    String, Text, Date, Enum as PgEnum, DateTime, ForeignKey, func, Index, UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from datetime import date, datetime
import enum

from app.core.database import Base


class CleaningRequestStatus(str, enum.Enum):
    """保洁进度：申请后进行中 → 打扫完了（撤码）。"""
    requested = "requested"
    cleaned = "cleaned"


class CleaningApprovalStatus(str, enum.Enum):
    """审批：待通过 → 前台/管家授权 / 驳回。"""
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class CleaningRequest(Base):
    """飞书「打扫申请群」自助保洁申请（PRD 打扫申请群-飞书自助-2026-07-12）。

    独立于 Task —— 刻意不复用 cleaning Task，避开退房打扫链路的房态联动
    （房里还住着人，绝不能把房态改成 available/cleaning）。
    生命周期两条正交轴：保洁进度(status) 与 审批(approval_status)，可任意先后。
    status=cleaned 是实际发生事实，立即记支出；approval_status 单独控制月结授权。
    """
    __tablename__ = "cleaning_requests"
    __table_args__ = (
        # 同房同日只一条打扫申请 —— DB 级去重键，挡并发/飞书重试双插（服务层靠它幂等）。
        UniqueConstraint("room_id", "request_date", name="uq_cleaning_requests_room_date"),
        # 用户要求保留 7 月旧账；从 8 月起数据库层禁止多个申请共用费用。
        Index(
            "uq_cleaning_requests_expense_id_from_202608",
            "expense_id",
            unique=True,
            postgresql_where=text("request_date >= DATE '2026-08-01'"),
            sqlite_where=text("request_date >= '2026-08-01'"),
        ),
        Index("ix_cleaning_requests_order", "order_id"),
    )

    request_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    room_id: Mapped[str] = mapped_column(String(10), ForeignKey("rooms.room_id"), nullable=False)
    order_id: Mapped[str] = mapped_column(String(20), ForeignKey("orders.order_id"), nullable=False)
    # 北京时间当天，同房同日去重键。
    request_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[CleaningRequestStatus] = mapped_column(
        PgEnum(CleaningRequestStatus, name="cleaning_request_status"),
        default=CleaningRequestStatus.requested,
    )
    approval_status: Mapped[CleaningApprovalStatus] = mapped_column(
        PgEnum(CleaningApprovalStatus, name="cleaning_approval_status"),
        default=CleaningApprovalStatus.pending,
    )
    requester_open_id: Mapped[str | None] = mapped_column(String(64))
    approver_open_id: Mapped[str | None] = mapped_column(String(64))
    # 打扫完成后生成的保洁费用。作废/审批/月结复核时溯源，一申请一条计费。
    expense_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("expenses.expense_id"))
    notes: Mapped[str | None] = mapped_column(Text)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
