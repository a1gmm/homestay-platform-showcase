from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from decimal import Decimal
from typing import Optional
import re


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{4,50}$")


def _normalize_username(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    if v == "":
        return None
    if not _USERNAME_RE.fullmatch(v):
        raise ValueError("账号需 4-50 位，仅允许字母、数字、下划线、点")
    return v.lower()


class OwnerCreate(BaseModel):
    owner_id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=50)
    username: Optional[str] = None
    phone: Optional[str] = None
    id_card: Optional[str] = None
    bank_account: Optional[str] = None
    bank_name: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("username")
    @classmethod
    def _norm_username(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_username(v)


class OwnerUpdate(BaseModel):
    name: Optional[str] = None
    username: Optional[str] = None
    phone: Optional[str] = None
    id_card: Optional[str] = None
    bank_account: Optional[str] = None
    bank_name: Optional[str] = None
    notes: Optional[str] = None
    parent_owner_id: Optional[str] = None  # "" 表示解绑上级

    @field_validator("username")
    @classmethod
    def _norm_username(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_username(v)

    @field_validator("parent_owner_id")
    @classmethod
    def _empty_to_none(cls, v: Optional[str]) -> Optional[str]:
        return None if (v is not None and v.strip() == "") else v


class OwnerRoomItem(BaseModel):
    room_id: str
    room_name: str
    owner_share_ratio: Decimal
    owner_deduction_rules: list[str] = []
    owner_ignored_categories: list[str] = []


class SubOwnerLite(BaseModel):
    owner_id: str
    name: str


class OwnerOut(BaseModel):
    owner_id: str
    name: str
    username: Optional[str]
    phone: Optional[str]
    id_card: Optional[str]
    bank_account: Optional[str]
    bank_name: Optional[str]
    notes: Optional[str]
    created_at: datetime
    parent_owner_id: Optional[str] = None
    is_master: bool = False
    sub_owners: list[SubOwnerLite] = []
    rooms: list[OwnerRoomItem] = []
    room_count: int = 0

    model_config = {"from_attributes": True}


class BatchAssignOwnerBody(BaseModel):
    room_ids: list[str]
    owner_id: Optional[str] = None       # None 表示解除业主关联
    owner_share_ratio: Optional[Decimal] = Field(default=None, ge=0, le=1)
    owner_deduction_rules: Optional[list[str]] = None
    owner_ignored_categories: Optional[list[str]] = None
