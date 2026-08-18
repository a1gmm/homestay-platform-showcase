"""
C-side (customer) authentication: phone + SMS OTP.

Endpoints:
- POST /customer/auth/send-otp   — generate & send OTP
- POST /customer/auth/verify     — verify OTP, issue customer JWT, upsert customer record
- GET  /customer/me              — current customer profile
- PATCH /customer/me             — update name / id_number
- GET  /customer/me/orders       — my orders (by phone)
"""
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from datetime import datetime, timezone

from app.core.deps import DBSession, RedisClient, CurrentCustomerPhone
from app.core.security import create_customer_token
from app.models.customer import Customer
from app.models.order import Order
from app.schemas.customer import (
    SendOtpRequest, VerifyOtpRequest, CustomerOut, CustomerToken, CustomerUpdate,
)
from app.schemas.order import OrderListItem
from app.services import otp_service, sms_service

router = APIRouter(prefix="/customer", tags=["customer"])


# ─── Auth ────────────────────────────────────────────────────────────────────

@router.post("/auth/send-otp")
async def send_otp(body: SendOtpRequest, request: Request, redis: RedisClient):
    try:
        body.validate_phone()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ip = request.client.host if request.client else None
    code = await otp_service.generate_and_store(redis, body.phone, ip)
    try:
        await sms_service.send_otp_sms(body.phone, code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"短信发送失败：{e}")
    return {"sent": True}


@router.post("/auth/verify", response_model=CustomerToken)
async def verify_otp(body: VerifyOtpRequest, db: DBSession, redis: RedisClient):
    ok = await otp_service.verify(redis, body.phone, body.code)
    if not ok:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    # Upsert customer
    result = await db.execute(select(Customer).where(Customer.phone == body.phone))
    customer = result.scalar_one_or_none()
    if customer is None:
        customer = Customer(phone=body.phone, name=body.name, last_login_at=datetime.now(timezone.utc))
        db.add(customer)
    else:
        customer.last_login_at = datetime.now(timezone.utc)
        if body.name and not customer.name:
            customer.name = body.name
    await db.commit()
    await db.refresh(customer)

    token, expires_in = create_customer_token(customer.phone)
    return CustomerToken(
        access_token=token,
        expires_in=expires_in,
        customer=CustomerOut.model_validate(customer),
    )


# ─── Me ──────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=CustomerOut)
async def get_me(phone: CurrentCustomerPhone, db: DBSession):
    result = await db.execute(select(Customer).where(Customer.phone == phone))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    return customer


@router.patch("/me", response_model=CustomerOut)
async def update_me(body: CustomerUpdate, phone: CurrentCustomerPhone, db: DBSession):
    result = await db.execute(select(Customer).where(Customer.phone == phone))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(customer, field, value)
    await db.commit()
    await db.refresh(customer)
    return customer


@router.get("/me/orders", response_model=list[OrderListItem])
async def my_orders(phone: CurrentCustomerPhone, db: DBSession):
    result = await db.execute(
        select(Order)
        .where(Order.guest_phone == phone, Order.is_deleted == False)
        .order_by(Order.created_at.desc())
        .limit(100)
    )
    orders = result.scalars().all()
    return [OrderListItem.model_validate(o) for o in orders]
