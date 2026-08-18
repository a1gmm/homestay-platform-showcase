"""
Unified auth —— 统一登录入口。

一个手机号 + OTP,扫 users/owners/customers 三张表收集所有匹配身份,
为每个身份签发对应 token,前端按需选择。

端点:
- POST /api/v1/auth/unified-send-otp  → 校验手机号至少在一张表,或允许自动注册 customer
- POST /api/v1/auth/unified-verify    → 返回 identities[] 含各自 token 和 redirect
"""
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Optional, Literal
import re

from app.core.deps import DBSession, RedisClient
from app.core.security import (
    create_access_token, create_refresh_token,
    create_owner_token, create_customer_token,
)
from app.models.user import User, UserRole
from app.models.owner import Owner
from app.models.customer import Customer
from app.services import otp_service, sms_service

router = APIRouter(prefix="/auth", tags=["unified-auth"])


def _validate_phone(phone: str) -> None:
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确")


class UnifiedSendOtp(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11)


class UnifiedVerifyOtp(BaseModel):
    phone: str
    code: str = Field(..., min_length=4, max_length=6)


class Identity(BaseModel):
    kind: Literal["user", "owner", "customer"]
    # common
    access_token: str
    redirect_to: str
    # user-specific
    role: Optional[str] = None
    user_id: Optional[str] = None
    display_name: Optional[str] = None
    refresh_token: Optional[str] = None
    # owner-specific
    owner_id: Optional[str] = None
    # customer/owner/user shared
    name: Optional[str] = None
    # 通用展示用
    label: Optional[str] = None  # "管家 · 李师傅"
    subtitle: Optional[str] = None  # 副标题, 如角色说明


class UnifiedVerifyResponse(BaseModel):
    phone: str
    identities: list[Identity]


def _redirect_for_user_role(role: str) -> str:
    if role in ("admin", "operator", "finance"):
        return "http://localhost:3000/dashboard"
    if role == "cleaner":
        return "/staff/cleaner"
    if role == "keeper":
        return "/staff/keeper"
    return "/staff"


_ROLE_LABEL = {
    "admin": "管理员",
    "operator": "运营",
    "finance": "财务",
    "cleaner": "保洁",
    "keeper": "管家",
    "owner": "业主",
}


@router.post("/unified-send-otp")
async def unified_send_otp(
    body: UnifiedSendOtp, request: Request, db: DBSession, redis: RedisClient,
):
    _validate_phone(body.phone)

    # 手机号是否在 user/owner 表中
    in_users = (await db.execute(
        select(User.user_id).where(User.phone == body.phone).where(User.is_active == True).limit(1)
    )).scalar_one_or_none()
    in_owners = (await db.execute(
        select(Owner.owner_id).where(Owner.phone == body.phone).limit(1)
    )).scalar_one_or_none()
    # 都不在也允许: 自动作为 customer 注册 (C 端订房场景)

    ip = request.client.host if request.client else None
    code = await otp_service.generate_and_store(redis, body.phone, ip)
    try:
        await sms_service.send_otp_sms(body.phone, code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"短信发送失败：{e}")
    return {"sent": True, "matched": bool(in_users or in_owners)}


@router.post("/unified-verify", response_model=UnifiedVerifyResponse)
async def unified_verify(body: UnifiedVerifyOtp, db: DBSession, redis: RedisClient):
    ok = await otp_service.verify(redis, body.phone, body.code)
    if not ok:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    identities: list[Identity] = []

    # 1) users (phone 可能对多个 active user,但 UNIQUE 索引限制了至多 1 个)
    user_rows = (await db.execute(
        select(User).where(User.phone == body.phone).where(User.is_active == True)
    )).scalars().all()
    for u in user_rows:
        access = create_access_token(u.user_id, u.role.value)
        refresh = create_refresh_token(u.user_id)
        role_lbl = _ROLE_LABEL.get(u.role.value, u.role.value)
        identities.append(Identity(
            kind="user",
            access_token=access,
            refresh_token=refresh,
            role=u.role.value,
            user_id=u.user_id,
            display_name=u.display_name,
            name=u.display_name,
            redirect_to=_redirect_for_user_role(u.role.value),
            label=f"{role_lbl} · {u.display_name}",
            subtitle=f"员工账号 · {role_lbl}",
        ))

    # 2) owners
    owner_rows = (await db.execute(
        select(Owner).where(Owner.phone == body.phone)
    )).scalars().all()
    for o in owner_rows:
        otoken, _ = create_owner_token(o.owner_id)
        identities.append(Identity(
            kind="owner",
            access_token=otoken,
            owner_id=o.owner_id,
            name=o.name,
            redirect_to="/owner",
            label=f"业主 · {o.name}",
            subtitle="查看名下房源账目与结算",
        ))

    # 3) customer —— 仅当 user/owner 都未命中时作为兜底身份自动创建
    if not identities:
        cust = (await db.execute(
            select(Customer).where(Customer.phone == body.phone)
        )).scalar_one_or_none()
        if cust is None:
            cust = Customer(phone=body.phone, last_login_at=datetime.now(timezone.utc))
            db.add(cust)
        else:
            cust.last_login_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(cust)
        ctoken, _ = create_customer_token(cust.phone)
        identities.append(Identity(
            kind="customer",
            access_token=ctoken,
            name=cust.name or "客户",
            redirect_to="/booking",
            label="客户 · 订房",
            subtitle="浏览房源并预订",
        ))

    return UnifiedVerifyResponse(phone=body.phone, identities=identities)
