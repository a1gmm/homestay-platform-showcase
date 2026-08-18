from pydantic import BaseModel, field_validator
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from app.models.order import Channel
from app.models.payment import PaymentMethod
from app.models.refund import RefundReason
from app.models.expense import ExpenseCategory, ExpensePayer


# Sanity upper bound: a single line item above 10M RMB almost always means a data entry error.
_MAX_AMOUNT = Decimal("10000000")


def _validate_positive_amount(value: Decimal) -> Decimal:
    if value <= 0:
        raise ValueError("金额必须大于 0")
    if value > _MAX_AMOUNT:
        raise ValueError("金额超过单笔上限")
    return value


# Payment
class PaymentCreate(BaseModel):
    order_id: str
    amount: Decimal
    method: PaymentMethod
    paid_at: datetime
    is_deposit: bool = False
    notes: Optional[str] = None

    @field_validator("amount")
    @classmethod
    def _amount_positive(cls, v: Decimal) -> Decimal:
        return _validate_positive_amount(v)


class PaymentUpdate(BaseModel):
    amount: Optional[Decimal] = None
    method: Optional[PaymentMethod] = None
    notes: Optional[str] = None

    @field_validator("amount")
    @classmethod
    def _amount_positive(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is None:
            return v
        return _validate_positive_amount(v)


class PaymentOut(BaseModel):
    payment_id: str
    order_id: str
    amount: Decimal
    method: PaymentMethod
    paid_at: datetime
    is_deposit: bool
    notes: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}


# Refund
class RefundCreate(BaseModel):
    order_id: str
    amount: Decimal
    reason: RefundReason
    refunded_at: datetime
    notes: Optional[str] = None

    @field_validator("amount")
    @classmethod
    def _amount_positive(cls, v: Decimal) -> Decimal:
        return _validate_positive_amount(v)


class RefundOut(BaseModel):
    refund_id: str
    order_id: str
    amount: Decimal
    reason: RefundReason
    refunded_at: datetime
    notes: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}


# Expense
class ExpenseCreate(BaseModel):
    category: ExpenseCategory
    amount: Decimal
    description: str
    expense_date: date
    room_id: Optional[str] = None
    order_id: Optional[str] = None
    payer: ExpensePayer = ExpensePayer.company
    owner_id: Optional[str] = None
    # 「整层」录入：前端传楼层号，服务端解析到该层唯一托管房业主，落库时
    # 只写 owner_id、room_id 留空（业主级支出，不拆到各房）。仅入参用，不进 Expense 模型。
    floor: Optional[int] = None
    notes: Optional[str] = None

    @field_validator("amount")
    @classmethod
    def _amount_positive(cls, v: Decimal) -> Decimal:
        return _validate_positive_amount(v)


class RenewalCleaningChargeCreate(BaseModel):
    """续住/在住房打扫登记。金额服务端固定（app.core.cleaning_pricing），
    前台只提供房间与订单，不能自定义金额。"""
    order_id: str
    room_id: str
    notes: Optional[str] = None


class ExpenseOut(BaseModel):
    expense_id: str
    category: ExpenseCategory
    amount: Decimal
    description: str
    expense_date: date
    room_id: Optional[str]
    order_id: Optional[str]
    payer: ExpensePayer
    owner_id: Optional[str]
    notes: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}


# By-room financial summary
class ByRoomSummaryItem(BaseModel):
    room_id: str
    room_name: str
    owner_id: Optional[str] = None
    owner_share_ratio: Decimal
    order_count: int
    revenue: Decimal              # 总房费
    commission: Decimal           # 平台佣金
    net_revenue: Decimal          # 净收入 = revenue - commission
    expense_company: Decimal      # 公司承担的支出
    expense_owner: Decimal        # 业主承担的支出
    owner_net_share: Decimal      # 业主应得 = (net_revenue - expense_owner) * share_ratio
    company_net: Decimal          # 公司净利 = net_revenue - expense_company - owner_net_share


# 单房明细「区间订单（收入来源）」：按 OrderRoom 段返回，口径同 summary_by_room 营收行。
# 多房订单顶层 room_id 为空，按段返回才能把该房参与的多房订单也列出来。
class RoomRevenueSegment(BaseModel):
    order_id: str
    order_room_id: str
    room_id: str
    guest_name: str
    channel: Channel
    check_in_date: date
    check_out_date: date
    nights: int
    actual_price: Optional[Decimal]   # 段级实收（OrderRoom.actual_price），非订单顶层
    model_config = {"from_attributes": True}


# Expense import
class ExpenseImportResult(BaseModel):
    succeeded: list[ExpenseOut]
    failed: list[dict]  # {row: int, errors: str, data: dict}
    total_rows: int
    imported_count: int


# ─── 业主服务费费率配置（财务页「费用标准设置」）─────────────────────────
class ServiceFeeConfigOut(BaseModel):
    checkout_cleaning_fee: Decimal          # 退房打扫 元/间/单
    instay_cleaning_fee: Decimal            # 续住/日常打扫 元/间/次
    laundry_fee_per_room: Decimal           # 洗涤 元/间/单
    consumable_fee_per_room_night: Decimal  # 日耗品 元/间/夜
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ServiceFeeConfigUpdate(BaseModel):
    checkout_cleaning_fee: Decimal
    instay_cleaning_fee: Decimal
    laundry_fee_per_room: Decimal
    consumable_fee_per_room_night: Decimal

    @field_validator(
        "checkout_cleaning_fee", "instay_cleaning_fee",
        "laundry_fee_per_room", "consumable_fee_per_room_night",
    )
    @classmethod
    def _non_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("费率不能为负")
        if v > _MAX_AMOUNT:
            raise ValueError("费率过大，疑似录入错误")
        return v
