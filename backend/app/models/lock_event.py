"""lock_events —— 慧享家 `lockLogPush` 开锁日志推送落库（plan §6.9 / PLAN_lock_auto_sync）。

每次锁被操作，厂商 push 一条日志到我们的回调，内含 `electricNum`（电量）。
一条 push 一行；`log_id`（厂商 base64 主键）唯一 → 幂等去重（重复推送忽略）。
仅审计 + 供电量/在线态自愈；不参与订单/房态判断。
"""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LockEvent(Base):
    __tablename__ = "lock_events"
    __table_args__ = (
        UniqueConstraint("log_id", name="uq_lock_events_log_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    log_id: Mapped[str] = mapped_column(String(64), nullable=False)   # 厂商 base64 去重键
    vendor_room_id: Mapped[str | None] = mapped_column(String(64))    # 厂商房间 base64
    lock_mac: Mapped[str | None] = mapped_column(String(32))
    log_type: Mapped[int | None] = mapped_column(Integer)
    log_level: Mapped[str | None] = mapped_column(String(16))         # DEBUG/INFO/WARN/ERROR
    log_alert: Mapped[str | None] = mapped_column(Text)              # 人类可读日志文案
    electric_num: Mapped[int | None] = mapped_column(SmallInteger)   # 电量百分比
    photo_url: Mapped[str | None] = mapped_column(Text)             # 开锁抓拍图 URL（仅存）
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 厂商 updateTime
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
