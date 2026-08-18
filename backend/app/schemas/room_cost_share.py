"""Pydantic schemas for v2-issue#2 — RoomCostShareRule API."""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator

from app.models.expense import ExpenseCategory
from app.models.order import BookingType


def _validate_share_percent(v: Decimal | None) -> Decimal | None:
    if v is None:
        return v
    if v < 0 or v > 1:
        raise ValueError("share_percent 必须在 0~1 之间")
    return v


class CostShareRuleItem(BaseModel):
    """单条规则的输入/输出（用于 list 端点和 bulk upsert payload）。"""
    booking_type: BookingType
    expense_category: ExpenseCategory
    share_percent: Decimal = Decimal("1.000")
    unit_price: Optional[Decimal] = None

    @field_validator("share_percent")
    @classmethod
    def _share_valid(cls, v):
        return _validate_share_percent(v)


class CostShareRuleOut(CostShareRuleItem):
    rule_id: str
    room_id: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CostShareRulesUpsertRequest(BaseModel):
    """单房间 upsert 一组规则。覆盖 (room_id, booking_type, expense_category) 三元组。"""
    rules: list[CostShareRuleItem]


class CostShareRulesBulkRequest(BaseModel):
    """批量给多个房间设同一组规则（v2-issue#3 房间编辑表单的"批量更改"按钮用）。"""
    room_ids: list[str]
    rules: list[CostShareRuleItem]
