"""Admin-only BYPMS synchronization operations read endpoints."""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_role
from app.schemas.bypms_sync import (
    BypmsSyncConflictsResponse,
    BypmsSyncCyclesResponse,
    BypmsSyncOverview,
    BypmsSyncRetryResponse,
    ConflictStatus,
)
from app.services.bypms_sync_admin import (
    RetryAlreadyActiveError,
    RetryRequestFailedError,
    RetryUnavailableError,
    enqueue_retry_request,
    get_overview,
    list_conflicts,
    list_cycles,
)
from app.services.manual_override import OrderSyncField


router = APIRouter(prefix="/system/bypms-sync", tags=["bypms-sync"])

_RETRY_UNAVAILABLE = {
    "code": "BYPMS_RETRY_UNAVAILABLE",
    "message": "宝寓同步重试暂不可用",
}
_RETRY_ACTIVE = {
    "code": "BYPMS_RETRY_ALREADY_ACTIVE",
    "message": "已有重试正在排队或执行",
}
_RETRY_FAILED = {
    "code": "BYPMS_RETRY_FAILED",
    "message": "重试请求失败",
}
_INVALID_IDEMPOTENCY_KEY = {
    "code": "IDEMPOTENCY_KEY_INVALID",
    "message": "Idempotency-Key 必须为 1–1024 字节的非空值",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/overview", response_model=BypmsSyncOverview)
async def overview(
    _current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> BypmsSyncOverview:
    return await get_overview(db, _utcnow())


@router.get("/cycles", response_model=BypmsSyncCyclesResponse)
async def cycles(
    limit: int = Query(default=20, ge=1, le=100),
    status: Literal["running", "succeeded", "partial", "failed", "skipped"] | None = None,
    _current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> BypmsSyncCyclesResponse:
    return await list_cycles(db, _utcnow(), limit=limit, status=status)


@router.get("/conflicts", response_model=BypmsSyncConflictsResponse)
async def conflicts(
    field: OrderSyncField | None = None,
    conflict_status: ConflictStatus = Query(
        default="open", alias="status"
    ),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=1, le=100),
    _current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> BypmsSyncConflictsResponse:
    return await list_conflicts(
        db,
        field=field.value if field is not None else None,
        status=conflict_status,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/retry",
    response_model=BypmsSyncRetryResponse,
    status_code=http_status.HTTP_201_CREATED,
)
async def retry(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> BypmsSyncRetryResponse:
    try:
        encoded = idempotency_key.encode("utf-8") if idempotency_key is not None else b""
    except UnicodeEncodeError:
        encoded = b""
    if (
        idempotency_key is None
        or not idempotency_key.strip()
        or not encoded
        or len(encoded) > 1024
    ):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_INVALID_IDEMPOTENCY_KEY,
        )
    try:
        return await enqueue_retry_request(
            db,
            raw_idempotency_key=idempotency_key,
            operator_id=current["user_id"],
        )
    except RetryUnavailableError:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_RETRY_UNAVAILABLE,
        ) from None
    except RetryAlreadyActiveError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=_RETRY_ACTIVE,
        ) from None
    except RetryRequestFailedError:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_RETRY_FAILED,
        ) from None
