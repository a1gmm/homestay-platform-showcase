"""door_codes —— 一把门锁密码的生命周期（见 plan §8.2 / §15.5 / §16）。

purpose: guest 客人码 / cleaning 保洁码 / keeper 管家码 / master 总码。
status:  pending 待下发 / active 已下到锁 / revoked 已作废 / expired 已过期 /
         failed 下码失败 / manual 人工模式待前台手动设置。
status 由 provider 的 IssueOutcome 映射：GRANTED→active、QUEUED→pending、FAILED→failed。

幂等（见 §16 决策2）：partial unique index 保证同一 (order_id, room_id, purpose)
在 active/pending 态只能有一把，防 handle_checkin 重复触发双码。
"""
import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum as PgEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DoorCodePurpose(str, enum.Enum):
    guest = "guest"
    cleaning = "cleaning"
    keeper = "keeper"
    master = "master"


class DoorCodeStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    revoked = "revoked"
    expired = "expired"
    failed = "failed"
    manual = "manual"


# 幂等条件：仅对「占用中」的码去重（已作废/过期/失败的不算）
_ACTIVE_STATES_SQL = "status IN ('pending', 'active')"


class DoorCode(Base):
    __tablename__ = "door_codes"
    __table_args__ = (
        Index(
            "uq_door_codes_active_per_order_room_purpose",
            "order_id",
            "room_id",
            "purpose",
            unique=True,
            sqlite_where=text(_ACTIVE_STATES_SQL),
            postgresql_where=text(_ACTIVE_STATES_SQL),
        ),
        Index("ix_door_codes_room_status_start", "room_id", "status", "start_at"),
        Index("ix_door_codes_order_purpose", "order_id", "purpose"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 保洁/管家/总码不挂订单 → nullable
    order_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("orders.order_id"), nullable=True
    )
    room_id: Mapped[str] = mapped_column(
        String(10), ForeignKey("rooms.room_id"), nullable=False
    )
    lock_device_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("lock_devices.id"), nullable=True
    )
    purpose: Mapped[DoorCodePurpose] = mapped_column(
        PgEnum(DoorCodePurpose, name="door_code_purpose"), nullable=False
    )
    phone_no: Mapped[str | None] = mapped_column(String(20))
    # 可空（pending 尚未生成）；长度容 Fernet 密文（at-rest 加密在 OrderLockService 决定）
    password: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[DoorCodeStatus] = mapped_column(
        PgEnum(DoorCodeStatus, name="door_code_status"),
        nullable=False,
        default=DoorCodeStatus.pending,
    )
    vendor_key_id: Mapped[str | None] = mapped_column(String(64))   # 作废/改期引用
    vendor_lock_key_id: Mapped[int | None] = mapped_column(Integer)
    vendor_key_group_id: Mapped[int | None] = mapped_column(Integer)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # 重推退避（安全网 §连环响事故）：retry_pending 每次未确认重推 +1，据此指数退避；
    # next_retry_at 未到不重推（别每 5 分钟骚扰已下好码的在线锁），下成功即清零。
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
