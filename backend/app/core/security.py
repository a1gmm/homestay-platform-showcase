from datetime import datetime, timedelta, timezone
from typing import Optional
import uuid
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ─────────────────────────── Password hashing ────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ─────────────────────────── JWT tokens ──────────────────────────────────────

def _new_jti() -> str:
    return uuid.uuid4().hex


def create_access_token(subject: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": subject, "role": role, "type": "access", "exp": expire, "jti": _new_jti()}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {"sub": subject, "type": "refresh", "exp": expire, "jti": _new_jti()}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


# ─────────────────────────── Customer (C-side) JWT ───────────────────────────
# 与 admin JWT 使用同一 secret 但 type 不同，避免跨端伪造。

def create_customer_token(phone: str) -> tuple[str, int]:
    """Returns (token, expires_in_seconds)."""
    expires_in = settings.CUSTOMER_JWT_EXPIRE_DAYS * 24 * 3600
    expire = datetime.now(timezone.utc) + timedelta(days=settings.CUSTOMER_JWT_EXPIRE_DAYS)
    payload = {"sub": phone, "type": "customer_access", "exp": expire}
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, expires_in


def decode_customer_token(token: str) -> Optional[str]:
    """Returns phone if valid customer token, else None."""
    payload = decode_token(token)
    if not payload or payload.get("type") != "customer_access":
        return None
    return payload.get("sub")


# ─────────────────────────── Owner (Owner portal) JWT ────────────────────────
# Owner JWT sub = owner_id（便于直接按 owner_id 过滤，不用每次 phone 查表）

def create_owner_token(owner_id: str) -> tuple[str, int]:
    expires_in = settings.CUSTOMER_JWT_EXPIRE_DAYS * 24 * 3600
    expire = datetime.now(timezone.utc) + timedelta(days=settings.CUSTOMER_JWT_EXPIRE_DAYS)
    payload = {"sub": owner_id, "type": "owner_access", "exp": expire}
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, expires_in


def decode_owner_token(token: str) -> Optional[str]:
    """Returns owner_id if valid owner token, else None."""
    payload = decode_token(token)
    if not payload or payload.get("type") != "owner_access":
        return None
    return payload.get("sub")
