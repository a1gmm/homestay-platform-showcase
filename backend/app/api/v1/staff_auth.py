"""
Staff portal 登录 —— 员工（保洁/管家/运营/管理员）用手机号 + OTP 登录。

与 /auth/login 的 username+password 并存。
认证成功后签发的 token 与 /auth/login 完全一致，复用现有 `CurrentUser` guard，
所有现有管理 API 无需改动。
"""
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select
from pydantic import BaseModel, Field
import re

from app.core.config import settings
from app.core.deps import DBSession, RedisClient
from app.core.security import create_access_token, create_refresh_token
from app.models.user import User
from app.schemas.auth import TokenResponse
from app.services import otp_service, sms_service

router = APIRouter(prefix="/staff", tags=["staff-portal"])


class StaffSendOtp(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11)


class StaffVerifyOtp(BaseModel):
    phone: str
    code: str = Field(..., min_length=4, max_length=6)


def _validate_phone(phone: str) -> None:
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确")


@router.post("/auth/send-otp")
async def staff_send_otp(body: StaffSendOtp, request: Request, db: DBSession, redis: RedisClient):
    _validate_phone(body.phone)

    exists = await db.execute(
        select(User.user_id).where(User.phone == body.phone).where(User.is_active == True)
    )
    if not exists.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="该手机号未绑定员工账号,请联系管理员")

    ip = request.client.host if request.client else None
    code = await otp_service.generate_and_store(redis, body.phone, ip)
    try:
        await sms_service.send_otp_sms(body.phone, code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"短信发送失败：{e}")
    return {"sent": True}


@router.post("/auth/verify", response_model=TokenResponse)
async def staff_verify_otp(
    body: StaffVerifyOtp, db: DBSession, redis: RedisClient, response: Response,
):
    ok = await otp_service.verify(redis, body.phone, body.code)
    if not ok:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    result = await db.execute(select(User).where(User.phone == body.phone))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="该手机号未绑定员工账号")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已禁用")

    access_token = create_access_token(user.user_id, user.role.value)
    refresh_token = create_refresh_token(user.user_id)

    response.set_cookie("access_token", access_token, httponly=True, secure=settings.APP_ENV == "production", samesite="lax", max_age=900)
    response.set_cookie("refresh_token", refresh_token, httponly=True, secure=settings.APP_ENV == "production", samesite="lax", max_age=7 * 86400)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user_id=user.user_id,
        role=user.role.value,
        display_name=user.display_name,
    )
