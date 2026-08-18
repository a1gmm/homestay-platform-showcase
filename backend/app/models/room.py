from sqlalchemy import String, SmallInteger, Integer, Numeric, Enum as PgEnum, DateTime, Date, Text, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime, date
from decimal import Decimal
import enum

from app.core.database import Base


class RoomStatus(str, enum.Enum):
    available = "available"
    occupied = "occupied"
    pending_clean = "pending_clean"   # 客人退房后,等待保洁认领
    cleaning = "cleaning"             # 保洁已点"开始打扫",清扫中
    maintenance = "maintenance"
    locked = "locked"
    reserved = "reserved"


class Room(Base):
    __tablename__ = "rooms"

    room_id: Mapped[str] = mapped_column(String(10), primary_key=True)
    room_name: Mapped[str] = mapped_column(String(50), nullable=False)
    # 房间分组（自由文本），如 "海之梦·高级海景大床房"。前端按此字段在甘特图分组渲染；
    # 未填则归入"未分组"。新增字段，老房间默认 NULL。
    room_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    floor: Mapped[int | None] = mapped_column(SmallInteger)
    owner_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("owners.owner_id"))
    # 普通订单业主分成比例（保留原字段名 owner_share_ratio 不改名以避免破坏 30+ 处现有引用）
    owner_share_ratio: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0.600"))
    # issue#6: 试住单 / 自住单 的业主分成比例（来自合同条款）。默认 1.0 = 业主全担。
    # 录入此类订单时，settlement 任务（Phase 2）会按对应比例从业主当月分成扣减。
    share_ratio_trial: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), default=Decimal("1.000"), server_default="1.000"
    )
    share_ratio_owner_self: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), default=Decimal("1.000"), server_default="1.000"
    )
    # 哪些支出类别从业主分成中扣除（e.g. ["cleaning","maintenance","platform_fee"]）
    owner_deduction_rules: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    # 哪些支出类别不计入月度结算（既不扣业主也不进月度报表）
    owner_ignored_categories: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    room_status: Mapped[RoomStatus] = mapped_column(
        PgEnum(RoomStatus, name="room_status"), default=RoomStatus.available
    )
    # 进入维修/锁房等状态前的上一个状态，用于"结束维修一键恢复"。
    # 每次 room_status 变化时由 PATCH 接口记录旧值（见 api/v1/rooms.py update_room）。
    # create_type=False：复用已存在的 room_status PG enum，避免重复建类型。
    previous_status: Mapped[RoomStatus | None] = mapped_column(
        PgEnum(RoomStatus, name="room_status", create_type=False), nullable=True
    )
    base_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # Location fields
    province: Mapped[str | None] = mapped_column(String(20))
    city: Mapped[str | None] = mapped_column(String(20))
    district: Mapped[str | None] = mapped_column(String(30))
    community_name: Mapped[str | None] = mapped_column(String(50))
    building_no: Mapped[str | None] = mapped_column(String(20))
    unit_no: Mapped[str | None] = mapped_column(String(10))
    # Feature 8: min stay & price rules
    min_stay_nights: Mapped[int] = mapped_column(Integer, default=1)
    weekend_markup: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    holiday_markup: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    channel_availability: Mapped[dict] = mapped_column(JSONB, default=dict)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    # 王总 2026-05-10 反馈：参考 PMS 截图需要登记的合同/上架日期 + 备注
    contract_signed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sale_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # 软删除（归档）：房间下线不物理删，只打标记。历史订单/收款/分账仍能 join
    # 出房名可溯源；房态列表/甘特/可订房/统计分母 一律过滤掉 is_deleted 房间。
    is_deleted: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 床位数：可在房间编辑页配置（2026-07-15）。洗涤费 = 洗涤单价 × 床位数（两床房算两份）。
    # 存 metadata.beds 免加列迁移（生产 alembic 双头，避开 DDL 风险，见 schema-three-sources）。
    # 缺省/脏值一律按 1，setter 保证 ≥1 且落成新 dict 触发 JSONB 变更追踪。
    @property
    def beds(self) -> int:
        try:
            n = int((self.metadata_ or {}).get("beds", 1))
        except (TypeError, ValueError):
            return 1
        return n if n >= 1 else 1

    @beds.setter
    def beds(self, value) -> None:
        try:
            n = max(1, int(value))
        except (TypeError, ValueError):
            n = 1
        self.metadata_ = {**(self.metadata_ or {}), "beds": n}

    # Relationships
    owner = relationship("Owner", back_populates="rooms")
    orders = relationship("Order", back_populates="room")
    images = relationship(
        "RoomImage",
        back_populates="room",
        cascade="all, delete-orphan",
        order_by="RoomImage.sort_order",
    )
