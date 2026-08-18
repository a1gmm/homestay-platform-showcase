"""lock_devices —— 系统房间 ↔ 厂商门锁设备映射（见 plan §8.2）。

一台锁一行；vendor_room_id 是厂商侧 base64 主键。设备入库时由 list_rooms 拉取后
人工映射到系统 room_id。
"""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LockDevice(Base):
    __tablename__ = "lock_devices"
    __table_args__ = (
        UniqueConstraint("vendor_name", "vendor_room_id", name="uq_lock_devices_vendor_room"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[str] = mapped_column(
        String(10), ForeignKey("rooms.room_id"), nullable=False
    )
    vendor_name: Mapped[str] = mapped_column(
        String(32), nullable=False, default="hxjiot", server_default="hxjiot"
    )
    vendor_room_id: Mapped[str] = mapped_column(String(64), nullable=False)  # base64
    vendor_lock_mac: Mapped[str | None] = mapped_column(String(32))
    last_online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_battery: Mapped[int | None] = mapped_column(SmallInteger)
    # 低电告警滞回闸：跌破阈值告警一次后置 True，回充到重置线才复位——防刷屏。
    battery_alerted: Mapped[bool] = mapped_column(
        default=False, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
