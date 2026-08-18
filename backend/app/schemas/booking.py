from pydantic import BaseModel, field_validator
from decimal import Decimal
from datetime import date
from typing import Optional


class RoomPublic(BaseModel):
    """公开房源信息，过滤所有敏感字段（房东/成本/门锁/内部备注）。"""
    room_id: str
    room_name: str
    floor: Optional[int] = None
    base_price: Optional[Decimal] = None
    province: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    community_name: Optional[str] = None
    min_stay_nights: int = 1
    # 展示用图片/描述/设施，从 metadata 里取
    images: list[str] = []
    description: Optional[str] = None
    amenities: list[str] = []

    model_config = {"from_attributes": True}

    @classmethod
    def from_room(cls, room, images: Optional[list[str]] = None) -> "RoomPublic":
        meta = getattr(room, "metadata_", None) or {}
        # Prefer explicitly-passed images (from room_images table).
        # Fall back to metadata.images legacy field so existing metadata-only
        # seed data keeps rendering until room_images is populated.
        final_images = images if images is not None else list(meta.get("images", []))
        if not final_images:
            final_images = list(meta.get("images", []))
        return cls(
            room_id=room.room_id,
            room_name=room.room_name,
            floor=room.floor,
            base_price=room.base_price,
            province=room.province,
            city=room.city,
            district=room.district,
            community_name=room.community_name,
            min_stay_nights=room.min_stay_nights or 1,
            images=final_images,
            description=meta.get("description"),
            amenities=list(meta.get("amenities", [])),
        )


class CalendarDay(BaseModel):
    date: date
    price: Optional[Decimal] = None
    available: bool


class BookingOrderCreate(BaseModel):
    room_id: str
    check_in_date: date
    check_out_date: date
    guest_name: str
    id_number: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("check_out_date")
    @classmethod
    def checkout_after_checkin(cls, v, info):
        if "check_in_date" in info.data and v <= info.data["check_in_date"]:
            raise ValueError("退房日期必须晚于入住日期")
        return v


class BookingOrderOut(BaseModel):
    order_id: str
    room_id: str
    check_in_date: date
    check_out_date: date
    nights: int
    guest_name: str
    guest_phone: str
    list_price: Optional[Decimal]
    actual_price: Optional[Decimal]
    order_status: str
    payment_status: str
    created_at: str
