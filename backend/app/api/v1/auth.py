from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, status, Response, Request, Depends, Cookie
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from pydantic import BaseModel, field_validator
from typing import Optional
import uuid

from app.core.deps import DBSession, CurrentUser, get_redis, require_role, RedisClient, bearer
from app.core.security import (
    verify_password,
    hash_password,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.core.config import settings
from app.core.token_revocation import revoke, is_revoked, remaining_ttl
from app.core.auth_handoff import store as handoff_store, consume as handoff_consume
from app.models.user import User
from app.schemas.auth import LoginRequest, TokenResponse, RefreshRequest, UserCreate, UserOut
from app.services.audit import log_action_tx

router = APIRouter(prefix="/auth", tags=["auth"])


MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300  # 5 minutes


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: DBSession, response: Response, request: Request, redis: RedisClient):
    # Rate limiting: max 5 attempts per IP per 5 minutes (graceful if Redis unavailable)
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"login_attempts:{client_ip}"
    try:
        attempts = await redis.get(rate_key)
        if attempts and int(attempts) >= MAX_LOGIN_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="登录尝试过于频繁，请5分钟后再试",
            )
    except HTTPException:
        raise
    except Exception:
        pass  # Redis down — skip rate limiting, don't block login

    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()
    # 统一处理所有失败场景，避免通过响应差异推断用户是否存在 / 是否被禁用。
    login_failed = (
        user is None
        or not user.is_active
        or not verify_password(body.password, user.hashed_password)
    )
    if login_failed:
        # Increment failed attempt counter (best-effort)
        try:
            pipe = redis.pipeline()
            pipe.incr(rate_key)
            pipe.expire(rate_key, LOGIN_WINDOW_SECONDS)
            await pipe.execute()
        except Exception:
            pass  # Redis down — skip rate tracking
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    # Clear rate limit on successful login (best-effort)
    try:
        await redis.delete(rate_key)
    except Exception:
        pass

    access_token = create_access_token(user.user_id, user.role.value)
    refresh_token = create_refresh_token(user.user_id)

    # Set httpOnly cookies
    response.set_cookie("access_token", access_token, httponly=True, secure=settings.APP_ENV == "production", samesite="lax", max_age=900)
    response.set_cookie("refresh_token", refresh_token, httponly=True, secure=settings.APP_ENV == "production", samesite="lax", max_age=7 * 86400)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user_id=user.user_id,
        role=user.role.value,
        display_name=user.display_name,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: DBSession, response: Response, redis: RedisClient):
    payload = decode_token(body.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token 无效")
    # Reject already-revoked refresh tokens (e.g., replay after logout / after rotation).
    if await is_revoked(redis, payload.get("jti")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token 已注销")
    result = await db.execute(select(User).where(User.user_id == payload["sub"]))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")

    # Rotate: revoke the old refresh token so it can't be reused (common replay defense).
    now_ts = datetime.now(timezone.utc).timestamp()
    await revoke(redis, payload.get("jti"), remaining_ttl(payload.get("exp"), now_ts))

    access_token = create_access_token(user.user_id, user.role.value)
    new_refresh_token = create_refresh_token(user.user_id)
    response.set_cookie("access_token", access_token, httponly=True, secure=settings.APP_ENV == "production", samesite="lax", max_age=900)
    response.set_cookie("refresh_token", new_refresh_token, httponly=True, secure=settings.APP_ENV == "production", samesite="lax", max_age=7 * 86400)

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        user_id=user.user_id,
        role=user.role.value,
        display_name=user.display_name,
    )


# ─── 跨子域登录:一次性交接码 (#46) ──────────────────────────────────────────
# token 不再放进跳转 URL。登录侧 create 换 code,目标子域 accept 用 code exchange 换回 token。

class HandoffCreateRequest(BaseModel):
    at: str                              # access token(必填,会校验合法性)
    rt: Optional[str] = None             # refresh token(admin 端需要;staff 端不传)
    kind: Optional[str] = None           # "staff" 或缺省(admin)
    uid: Optional[str] = None
    role: Optional[str] = None
    name: Optional[str] = None
    next: Optional[str] = None


class HandoffCreateResponse(BaseModel):
    code: str


class HandoffExchangeRequest(BaseModel):
    code: str


@router.post("/handoff/create", response_model=HandoffCreateResponse)
async def handoff_create(body: HandoffCreateRequest, redis: RedisClient):
    """暂存登录 token,换取一次性交接码。仅接受合法 access token,避免被当成任意 KV。"""
    payload = decode_token(body.at)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="access token 无效")
    try:
        code = await handoff_store(redis, body.model_dump(exclude_none=True))
    except Exception:
        # Redis 不可用时返回 503;前端据此降级回「URL 携带 token」的老方式,保证登录不被 Redis 拖死。
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="登录交接暂不可用")
    return HandoffCreateResponse(code=code)


@router.post("/handoff/exchange")
async def handoff_exchange(body: HandoffExchangeRequest, redis: RedisClient):
    """用一次性交接码换回 token + 身份。无效/过期/已用返回 401。"""
    data = await handoff_consume(redis, body.code)
    if not data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="交接码无效或已过期")
    return data


@router.post("/logout")
async def logout(
    response: Response,
    current_user: CurrentUser,
    redis: RedisClient,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    access_token: Optional[str] = Cookie(default=None),
    refresh_token: Optional[str] = Cookie(default=None),
    body: Optional[RefreshRequest] = None,
):
    """Revoke both access and refresh tokens on logout.

    Access token is taken from the Authorization header or `access_token` cookie (whichever
    was used to authenticate this call). Refresh token comes from the cookie or an optional
    request body — either form is accepted so the front-end can migrate gradually.
    """
    now_ts = datetime.now(timezone.utc).timestamp()

    raw_access = credentials.credentials if credentials else access_token
    if raw_access:
        p = decode_token(raw_access)
        if p and p.get("jti"):
            await revoke(redis, p["jti"], remaining_ttl(p.get("exp"), now_ts))

    raw_refresh = refresh_token or (body.refresh_token if body else None)
    if raw_refresh:
        p = decode_token(raw_refresh)
        if p and p.get("jti"):
            await revoke(redis, p["jti"], remaining_ttl(p.get("exp"), now_ts))

    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return {"message": "已退出登录"}


@router.get("/me", response_model=UserOut)
async def me(current_user: CurrentUser, db: DBSession):
    result = await db.execute(select(User).where(User.user_id == current_user["user_id"]))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    db: DBSession,
    current_user: CurrentUser,
):
    # Only admin can create users
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可创建用户")
    # 使用 UUID 生成稳定的用户 ID，避免 ulid 版本差异导致的报错
    user_id = "USR-" + uuid.uuid4().hex[:12].upper()
    user = User(
        user_id=user_id,
        username=body.username,
        display_name=body.display_name,
        hashed_password=hash_password(body.password),
        role=body.role,
        phone=body.phone,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)  # 取 is_active 等默认值，供审计快照
    await log_action_tx(
        db,
        operator_id=current_user["user_id"],
        action="create_user",
        resource_type="user",
        resource_id=user_id,
        after_data={"username": user.username, "role": user.role.value, "is_active": user.is_active},
    )
    await db.commit()
    return user


@router.get("/users", response_model=list[UserOut])
async def list_users(db: DBSession, current_user: CurrentUser):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可查看用户列表")
    result = await db.execute(select(User).order_by(User.created_at))
    return result.scalars().all()


class UserStatusUpdate(BaseModel):
    is_active: bool


class ResetPasswordBody(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("密码长度不能少于8位")
        if v.isdigit() or v.isalpha():
            raise ValueError("密码必须包含字母和数字")
        return v


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user_status(
    user_id: str,
    body: UserStatusUpdate,
    db: DBSession,
    current_user: CurrentUser,
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改用户状态")

    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    before = {"is_active": user.is_active}
    user.is_active = body.is_active
    await log_action_tx(
        db,
        operator_id=current_user["user_id"],
        action="update_user_status",
        resource_type="user",
        resource_id=user_id,
        before_data=before,
        after_data={"is_active": user.is_active},
    )
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    body: ResetPasswordBody,
    db: DBSession,
    current_user: CurrentUser,
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可重置密码")

    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    user.hashed_password = hash_password(body.new_password)
    await log_action_tx(
        db,
        operator_id=current_user["user_id"],
        action="reset_password",
        resource_type="user",
        resource_id=user_id,
        notes="管理员重置用户密码",
    )
    await db.commit()
    return {"message": "密码已重置"}


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("密码长度不能少于8位")
        if v.isdigit() or v.isalpha():
            raise ValueError("密码必须包含字母和数字")
        return v


@router.post("/me/password")
async def change_my_password(
    body: ChangePasswordBody,
    db: DBSession,
    current_user: CurrentUser,
):
    """当前登录用户自助修改密码（需提供当前密码）。"""
    result = await db.execute(select(User).where(User.user_id == current_user["user_id"]))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    if not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(status_code=401, detail="当前密码错误")

    if body.current_password == body.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")

    user.hashed_password = hash_password(body.new_password)
    await log_action_tx(
        db,
        operator_id=current_user["user_id"],
        action="change_password",
        resource_type="user",
        resource_id=user.user_id,
        notes="用户自助修改密码",
    )
    await db.commit()
    return {"message": "密码已修改"}
