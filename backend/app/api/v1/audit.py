from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from typing import Optional

from app.core.deps import DBSession, CurrentUser
from app.models.audit_log import AuditLog
from pydantic import BaseModel
from datetime import date, datetime

from app.core.datetime_helpers import cn_date_range_utc

router = APIRouter(prefix="/audit-logs", tags=["audit"])


class AuditLogOut(BaseModel):
    log_id: int
    operator_id: Optional[str]
    action: str
    resource_type: Optional[str]
    resource_id: Optional[str]
    ip_address: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}


@router.get("", response_model=list[AuditLogOut])
async def list_audit_logs(
    db: DBSession,
    current_user: CurrentUser,
    resource_type: Optional[str] = Query(default=None),
    resource_id: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    # 操作日志：admin / operator / keeper / finance 可查；cleaner / owner 不开放
    if current_user["role"] not in ("admin", "operator", "keeper", "finance"):
        raise HTTPException(status_code=403, detail="无权查看操作日志")

    if (date_from is None) != (date_to is None):
        raise HTTPException(status_code=422, detail="date_from 和 date_to 必须同时提供")
    if date_from is not None and date_to is not None and date_to < date_from:
        raise HTTPException(status_code=422, detail="date_to 不能早于 date_from")

    q = select(AuditLog)
    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    if resource_id:
        q = q.where(AuditLog.resource_id == resource_id)
    if date_from is not None and date_to is not None:
        start_utc, end_utc = cn_date_range_utc(date_from, date_to)
        q = q.where(AuditLog.created_at >= start_utc, AuditLog.created_at < end_utc)

    q = q.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all()
