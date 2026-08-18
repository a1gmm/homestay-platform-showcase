from fastapi import Depends, HTTPException, status, Cookie
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Annotated
import redis.asyncio as aioredis

from app.core.security import decode_token, decode_customer_token, decode_owner_token
from app.core.token_revocation import is_revoked
from app.core.config import settings
from app.core.database import AsyncSessionLocal

bearer = HTTPBearer(auto_error=False)


# ─────────────────────────── DB session ──────────────────────────────────────

async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


DBSession = Annotated[AsyncSession, Depends(get_db)]


# ─────────────────────────── Redis ───────────────────────────────────────────

_redis_pool: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_pool


RedisClient = Annotated[aioredis.Redis, Depends(get_redis)]


# ─────────────────────────── Auth ────────────────────────────────────────────

def _extract_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    access_token: Optional[str] = Cookie(default=None),
) -> str:
    if credentials:
        return credentials.credentials
    if access_token:
        return access_token
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")


async def get_current_user(
    token: str = Depends(_extract_token),
    redis: aioredis.Redis = Depends(get_redis),
):
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效或已过期")
    if await is_revoked(redis, payload.get("jti")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 已注销")
    role = payload.get("role")
    if not role:
        # 缺 role claim 的 token（旧格式 / 跨端伪造尝试）一律拒绝，绝不默认到 operator。
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效")
    return {"user_id": payload["sub"], "role": role}


CurrentUser = Annotated[dict, Depends(get_current_user)]


def require_role(*roles: str):
    """Dependency factory — usage: Depends(require_role('admin'))"""
    async def _checker(current_user: CurrentUser):
        if current_user["role"] not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")
        return current_user
    return _checker


# ─────────────────────────── Customer (C-side) Auth ──────────────────────────

def _extract_customer_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    customer_access_token: Optional[str] = Cookie(default=None),
) -> str:
    if credentials:
        return credentials.credentials
    if customer_access_token:
        return customer_access_token
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")


async def get_current_customer_phone(token: str = Depends(_extract_customer_token)) -> str:
    phone = decode_customer_token(token)
    if not phone:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")
    return phone


CurrentCustomerPhone = Annotated[str, Depends(get_current_customer_phone)]


# ─────────────────────────── Owner (Owner portal) Auth ───────────────────────

def _extract_owner_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    owner_access_token: Optional[str] = Cookie(default=None),
) -> str:
    if credentials:
        return credentials.credentials
    if owner_access_token:
        return owner_access_token
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")


async def get_current_owner_id(token: str = Depends(_extract_owner_token)) -> str:
    owner_id = decode_owner_token(token)
    if not owner_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")
    return owner_id


CurrentOwnerId = Annotated[str, Depends(get_current_owner_id)]
