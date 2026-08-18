from pydantic import BaseModel
from decimal import Decimal
from typing import Optional, Any
from datetime import date
from app.models.room import RoomStatus


class RoomCreate(BaseModel):
    room_id: str
    room_name: str
    room_type: Optional[str] = None  # 房间分组（自由文本），用于甘特图分组渲染
    floor: Optional[int] = None
    beds: Optional[int] = None  # 床位数（≥1，缺省 1）；洗涤费 = 单价 × 床位数
    owner_id: Optional[str] = None
    owner_share_ratio: Decimal = Decimal("0.600")
    # issue#6: 试住/自住单分成比例，业主合同里约定的；默认 1.0 = 业主全担
    share_ratio_trial: Decimal = Decimal("1.000")
    share_ratio_owner_self: Decimal = Decimal("1.000")
    base_price: Optional[Decimal] = None
    province: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    community_name: Optional[str] = None
    building_no: Optional[str] = None
    unit_no: Optional[str] = None
    contract_signed_date: Optional[date] = None
    sale_date: Optional[date] = None
    remarks: Optional[str] = None


class RoomUpdate(BaseModel):
    room_name: Optional[str] = None
    room_type: Optional[str] = None  # 房间分组（自由文本）
    floor: Optional[int] = None
    beds: Optional[int] = None  # 床位数（≥1）；洗涤费 = 单价 × 床位数
    owner_id: Optional[str] = None
    owner_share_ratio: Optional[Decimal] = None
    # issue#6
    share_ratio_trial: Optional[Decimal] = None
    share_ratio_owner_self: Optional[Decimal] = None
    room_status: Optional[RoomStatus] = None
    base_price: Optional[Decimal] = None
    province: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    community_name: Optional[str] = None
    building_no: Optional[str] = None
    unit_no: Optional[str] = None
    # Feature 8: min stay & price rules
    min_stay_nights: Optional[int] = None
    weekend_markup: Optional[Decimal] = None
    holiday_markup: Optional[Decimal] = None
    contract_signed_date: Optional[date] = None
    sale_date: Optional[date] = None
    remarks: Optional[str] = None


class RoomOut(BaseModel):
    room_id: str
    room_name: str
    room_type: Optional[str] = None  # 房间分组
    floor: Optional[int]
    owner_id: Optional[str]
    owner_share_ratio: Decimal
    # issue#6: 试住/自住单分成比例
    share_ratio_trial: Decimal = Decimal("1.000")
    share_ratio_owner_self: Decimal = Decimal("1.000")
    room_status: RoomStatus
    beds: int = 1  # 床位数（房间编辑页可配置）；洗涤费 = 单价 × 床位数
    # 上一个状态（进入维修/锁房前），用于前端"一键恢复"。由服务端维护，RoomUpdate 不暴露。
    previous_status: Optional[RoomStatus] = None
    base_price: Optional[Decimal]
    min_stay_nights: int = 1
    weekend_markup: Decimal = Decimal("0")
    holiday_markup: Decimal = Decimal("0")
    channel_availability: dict
    province: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    community_name: Optional[str] = None
    building_no: Optional[str] = None
    unit_no: Optional[str] = None
    contract_signed_date: Optional[date] = None
    sale_date: Optional[date] = None
    remarks: Optional[str] = None
    effective_status: str | None = None

    model_config = {"from_attributes": True}


class AvailabilityCheckRequest(BaseModel):
    room_id: str
    check_in_date: str
    check_out_date: str
    exclude_order_id: Optional[str] = None


class RoomPricingPoint(BaseModel):
    date: date
    recommended_price: Optional[Decimal] = None
    base_price: Optional[Decimal] = None
    competitor_avg_price: Optional[Decimal] = None
    source: Optional[str] = None
    source_url: Optional[str] = None


# issue#7: 批量设置房间分成比例
class BulkShareRatiosBody(BaseModel):
    """三类分成比例任一字段为 None 即跳过该字段（仅覆盖请求中明确传的字段）。"""
    normal: Optional[Decimal] = None  # 普通单 → owner_share_ratio
    trial: Optional[Decimal] = None  # 试住单 → share_ratio_trial
    owner_self: Optional[Decimal] = None  # 自住单 → share_ratio_owner_self


class BulkShareRatiosRequest(BaseModel):
    room_ids: list[str]
    ratios: BulkShareRatiosBody
