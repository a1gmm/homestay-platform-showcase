from pydantic import BaseModel
from datetime import datetime
from decimal import Decimal
from typing import Optional


class GuestOut(BaseModel):
    guest_id: str
    name: str
    phone: str
    id_number: Optional[str] = None
    wechat: Optional[str] = None
    notes: Optional[str] = None
    visit_count: int
    total_spent: Decimal
    total_nights: int
    last_check_in: Optional[datetime] = None
    preferred_room: Optional[str] = None
    tags: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class GuestUpdate(BaseModel):
    name: Optional[str] = None
    id_number: Optional[str] = None
    wechat: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[str] = None
