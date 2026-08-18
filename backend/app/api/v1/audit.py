from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from typing import Optional

from app.core.deps import DBSession, CurrentUser
from app.models.audit_log import AuditLog
from pydantic import BaseModel
from datetime import datetime

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
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    # 操作日志：admin / operator / keeper / finance 可查；cleaner / owner 不开放
    if current_user["role"] not in ("admin", "operator", "keeper", "finance"):
        raise HTTPException(status_code=403, detail="无权查看操作日志")

    q = select(AuditLog)
    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    if resource_id:
        q = q.where(AuditLog.resource_id == resource_id)

    q = q.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all()
