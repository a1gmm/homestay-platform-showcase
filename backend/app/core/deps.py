from fastapi import Depends, HTTPException, Request, status, Cookie
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Annotated
import redis.asyncio as aioredis

from app.core.security import decode_token, decode_customer_token, decode_owner_token
from app.core.token_revocation import is_revoked
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.user import User

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


async def get_current_db_user(current_user: CurrentUser, db: DBSession) -> User:
    """Reload the authenticated employee from the authoritative database."""
    user = await db.scalar(
        select(User)
        .where(User.user_id == current_user["user_id"])
        .execution_options(populate_existing=True)
    )
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="权限不足",
        )
    return user


CurrentDBUser = Annotated[User, Depends(get_current_db_user)]


def require_monthly_close_action(action, *, low_role_upload_entrypoint: bool = False):
    """Authorize one typed monthly-close action using current DB identity.

    The dependency binds the route's exact month/source/subject identifiers.
    Services that load a proposal, document, or issue still reauthorize the
    loaded object before any write; this route gate is never the only control.
    """

    async def _checker(
        request: Request,
        current_user: CurrentUser,
        db: DBSession,
    ):
        from sqlalchemy import select

        from app.models.monthly_close import (
            MONTHLY_CLOSE_SOURCE_TYPES,
            MonthlyCloseCycle,
            MonthlyCloseDocument,
            MonthlyCloseInboxItem,
            MonthlyCloseIntakeLink,
        )
        from app.models.monthly_close_control import (
            MonthlyCloseExecutionAttempt,
            MonthlyCloseIssueInstance,
            MonthlyCloseProposal,
        )
        from app.services.monthly_close.permissions import (
            MonthlyClosePermissionContext,
            MonthlyClosePermissionDenied,
            authorize_current_monthly_close_actor,
        )

        billing_month = str(request.path_params.get("billing_month") or "global")
        cycle_id = billing_month
        if billing_month != "global":
            stored_cycle_id = await db.scalar(
                select(MonthlyCloseCycle.cycle_id).where(
                    MonthlyCloseCycle.billing_month == billing_month
                )
            )
            if stored_cycle_id is not None:
                cycle_id = str(stored_cycle_id)
        source_type = request.path_params.get("source_type") or request.query_params.get(
            "source_type"
        )
        subject_type = None
        subject_id = None
        subject_cycle_id = None
        assigned_to = None
        uploaded_by = None
        owner_id = None
        for candidate_type, candidate_name in (
            ("document", "document_id"),
            ("inbox", "item_id"),
            ("proposal", "proposal_id"),
            ("attempt", "attempt_id"),
            ("intake_link", "link_id"),
            ("issue", "issue_id"),
        ):
            value = request.path_params.get(candidate_name)
            if value is not None:
                subject_type = candidate_type
                subject_id = str(value)
                break
        subject = None
        if subject_type == "document":
            subject = await db.get(MonthlyCloseDocument, subject_id)
            if subject is not None:
                source_type = subject.source_type
                uploaded_by = subject.uploaded_by
        elif subject_type == "inbox":
            subject = await db.get(MonthlyCloseInboxItem, subject_id)
            if subject is not None:
                source_type = subject.source_type
                uploaded_by = subject.created_by
        elif subject_type == "proposal":
            subject = await db.get(MonthlyCloseProposal, subject_id)
        elif subject_type == "attempt":
            subject = await db.get(MonthlyCloseExecutionAttempt, subject_id)
        elif subject_type == "intake_link":
            subject = await db.get(MonthlyCloseIntakeLink, subject_id)
            if subject is not None:
                source_type = subject.source_type
        elif subject_type == "issue":
            subject = await db.get(MonthlyCloseIssueInstance, subject_id)
            if subject is not None:
                source_type = (
                    subject.adapter_type
                    if subject.adapter_type in MONTHLY_CLOSE_SOURCE_TYPES
                    else None
                )
                assigned_to = subject.assigned_to
                owner_id = subject.assigned_to
        if subject is not None:
            subject_cycle_id = str(subject.cycle_id)
            if billing_month == "global":
                cycle_id = subject_cycle_id
        try:
            actor = await authorize_current_monthly_close_actor(
                db,
                current_user,
                action,
                MonthlyClosePermissionContext(
                    cycle_id=cycle_id,
                    subject_cycle_id=subject_cycle_id,
                    source_type=str(source_type) if source_type is not None else None,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    assigned_to=assigned_to,
                    uploaded_by=uploaded_by,
                    owner_id=owner_id,
                    low_role_upload_entrypoint=low_role_upload_entrypoint,
                ),
            )
        except MonthlyClosePermissionDenied as exc:
            if (
                action == "view_cycle"
                and subject_id is not None
                and exc.code
                in {
                    "monthly_close_subject_not_found",
                    "monthly_close_source_forbidden",
                }
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "code": "projection_object_not_found",
                        "message": "对象不存在",
                    },
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                # Keep authorization failures indistinguishable at the HTTP
                # boundary.  Typed codes remain available to trusted service
                # callers, but returning them here could reveal whether an
                # exact cycle, document, or assignment exists.
                detail="权限不足",
            ) from exc
        return {
            "user_id": actor.user_id,
            "role": actor.role.value,
            "is_active": True,
        }

    return _checker


def require_role(*roles: str):
    """Dependency factory — usage: Depends(require_role('admin'))"""
    async def _checker(current_user: CurrentUser):
        if current_user["role"] not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")
        return current_user
    return _checker


def require_current_db_role(*roles: str):
    """Authorize a sensitive request from the current database user record.

    Access-token roles are intentionally treated only as authentication context:
    a role change or deactivation must take effect without waiting for the token
    to expire.
    """

    async def _checker(current_user: CurrentUser, db: DBSession):
        user = await db.get(User, current_user["user_id"])
        if user is None or not user.is_active or user.role.value not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )
        return {"user_id": user.user_id, "role": user.role.value}

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
