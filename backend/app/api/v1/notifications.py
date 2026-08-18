from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update
from typing import Optional
from pydantic import BaseModel
from datetime import datetime, timezone
import uuid

from app.core.config import settings
from app.core.deps import DBSession, CurrentUser
from app.models.notification import Notification, NotificationLog, NotificationType
from app.models.order import Order
from app.models.room import Room
from app.models.door_code import DoorCode, DoorCodePurpose, DoorCodeStatus
from app.services.lock.code_crypto import decrypt_code

router = APIRouter(prefix="/notifications", tags=["notifications"])

import logging
_logger = logging.getLogger(__name__)


async def _active_guest_code(db, order_id: str) -> Optional[str]:
    """该订单最近一把 active 客人码的明文（plan §8.5 半自动）。
    Fail-safe：任何查不到/异常都返回 None，绝不让通知报错。"""
    try:
        row = (await db.execute(
            select(DoorCode).where(
                DoorCode.order_id == order_id,
                DoorCode.purpose == DoorCodePurpose.guest,
                DoorCode.status == DoorCodeStatus.active,
                DoorCode.password.isnot(None),
            ).order_by(DoorCode.created_at.desc())
        )).scalars().first()
        if row and row.password:
            return decrypt_code(row.password)  # 列存 Fernet 密文（§8.2），解密后填指引
    except Exception:
        _logger.exception("auto door-code lookup failed order=%s", order_id)
    return None


# ─── Schemas ─────────────────────────────────────────────────────────────────

class CheckinGuideRequest(BaseModel):
    order_id: str
    door_code: Optional[str] = None
    wifi_name: Optional[str] = None
    wifi_password: Optional[str] = None
    address: Optional[str] = None
    extra_notes: Optional[str] = None


class CheckinGuideResponse(BaseModel):
    success: bool
    log_id: str
    message: str


class NotificationTemplateOut(BaseModel):
    name: str
    description: str
    channels: list[str]
    variables: list[str]


class NotificationOut(BaseModel):
    notification_id: str
    user_id: Optional[str]
    title: str
    content: Optional[str]
    type: str
    is_read: bool
    created_at: datetime
    model_config = {"from_attributes": True}


# ─── Feature 3: Checkin guide & templates ─────────────────────────────────────

NOTIFICATION_TEMPLATES = [
    {
        "name": "checkin_guide",
        "description": "入住指南 — 包含门锁密码、WiFi、地址等信息",
        "channels": ["manual", "sms", "wechat"],
        "variables": ["guest_name", "room_name", "door_code", "wifi_name", "wifi_password", "address", "check_in_date", "check_out_date"],
    },
    {
        "name": "checkout_reminder",
        "description": "退房提醒 — 提醒客人退房时间和注意事项",
        "channels": ["manual", "sms", "wechat"],
        "variables": ["guest_name", "room_name", "check_out_date"],
    },
    {
        "name": "deposit_collected",
        "description": "押金收取确认",
        "channels": ["manual", "sms"],
        "variables": ["guest_name", "deposit_amount"],
    },
    {
        "name": "deposit_returned",
        "description": "押金退还通知",
        "channels": ["manual", "sms"],
        "variables": ["guest_name", "deposit_amount"],
    },
    {
        "name": "cleaning_assigned",
        "description": "保洁任务分配通知",
        "channels": ["manual"],
        "variables": ["room_name", "assignee_name", "deadline"],
    },
]


@router.get("/templates", response_model=list[NotificationTemplateOut])
async def list_templates(current_user: CurrentUser):
    """列出所有通知模板"""
    return NOTIFICATION_TEMPLATES


@router.post("/send-checkin-guide", response_model=CheckinGuideResponse)
async def send_checkin_guide(
    body: CheckinGuideRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """发送入住指南（门锁密码、WiFi、地址等）"""
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="无权发送通知")

    result = await db.execute(
        select(Order).where(Order.order_id == body.order_id, Order.is_deleted == False)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    room_name = order.room_id or "待排房"
    if order.room_id:
        room_result = await db.execute(select(Room).where(Room.room_id == order.room_id))
        room = room_result.scalar_one_or_none()
        if room:
            room_name = room.room_name

    # 门锁密码：前台显式传入则用之；否则自动取该订单已下发的客人码（plan §8.5 半自动）。
    door_code = body.door_code or await _active_guest_code(db, order.order_id)

    # Build message content
    lines = [
        f"客人：{order.guest_name}",
        f"房间：{room_name}",
        f"入住：{order.check_in_date} → {order.check_out_date}",
    ]
    if door_code:
        lines.append(f"门锁密码：{door_code}")
    if body.wifi_name:
        lines.append(f"WiFi名称：{body.wifi_name}")
    if body.wifi_password:
        lines.append(f"WiFi密码：{body.wifi_password}")
    if body.address:
        lines.append(f"地址：{body.address}")
    if body.extra_notes:
        lines.append(f"备注：{body.extra_notes}")

    content = "\n".join(lines)

    # 飞书通道已下线 — 仅记录日志，不实际推送。后续如需 SMS / 微信通道再接入。
    log_id = "NL-" + uuid.uuid4().hex[:12].upper()
    log_entry = NotificationLog(
        log_id=log_id,
        order_id=body.order_id,
        template_name="checkin_guide",
        channel="manual",
        recipient=order.guest_phone,
        content=content,
        status="manual",
        error_message=None,
    )
    db.add(log_entry)
    await db.commit()

    return CheckinGuideResponse(
        success=True,
        log_id=log_id,
        message="入住指南已记录（飞书通道已下线，请手动转告客人）",
    )


# ─── Feature J: Printable check-in card ───────────────────────────────────────

@router.get("/checkin-card/{order_id}", response_class=HTMLResponse)
async def checkin_card(
    order_id: str,
    db: DBSession,
    current_user: CurrentUser,
):
    """返回可打印的入住凭证 HTML 页面。需登录的内部员工才能调用——含客人姓名/房号/地址。"""
    if current_user["role"] not in ("admin", "operator", "keeper", "finance"):
        raise HTTPException(status_code=403, detail="无权查看入住凭证")
    result = await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    room_name = order.room_id or "待排房"
    address = ""
    if order.room_id:
        room_result = await db.execute(select(Room).where(Room.room_id == order.room_id))
        room = room_result.scalar_one_or_none()
        if room:
            room_name = room.room_name
            parts = [p for p in [room.province, room.city, room.district, room.community_name, room.building_no, room.unit_no] if p]
            address = "".join(parts)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>入住凭证 - {order.guest_name}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; color: #333; padding: 40px; background: #f5f5f5; }}
  .card {{ max-width: 600px; margin: 0 auto; background: #fff; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); overflow: hidden; }}
  .header {{ background: linear-gradient(135deg, #1677ff 0%, #0958d9 100%); color: #fff; padding: 32px 40px; text-align: center; }}
  .header h1 {{ font-size: 24px; margin-bottom: 4px; }}
  .header p {{ font-size: 14px; opacity: 0.85; }}
  .body {{ padding: 32px 40px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  tr {{ border-bottom: 1px solid #f0f0f0; }}
  tr:last-child {{ border-bottom: none; }}
  td {{ padding: 14px 0; vertical-align: top; }}
  td.label {{ width: 100px; color: #8c8c8c; font-size: 14px; }}
  td.value {{ font-size: 15px; font-weight: 500; color: #262626; }}
  .highlight {{ background: #f6ffed; border: 1px solid #b7eb8f; border-radius: 8px; padding: 16px 20px; margin-top: 20px; }}
  .highlight .label {{ color: #52c41a; font-weight: 600; font-size: 13px; margin-bottom: 6px; }}
  .highlight .code {{ font-size: 28px; font-weight: 700; letter-spacing: 4px; color: #389e0d; }}
  .footer {{ text-align: center; padding: 20px 40px 32px; color: #bfbfbf; font-size: 12px; }}
  @media print {{
    body {{ padding: 0; background: #fff; }}
    .card {{ box-shadow: none; border-radius: 0; max-width: 100%; }}
    .no-print {{ display: none !important; }}
  }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <h1>入住凭证</h1>
    <p>{settings.PROJECT_NAME}</p>
  </div>
  <div class="body">
    <table>
      <tr><td class="label">客人姓名</td><td class="value">{order.guest_name}</td></tr>
      <tr><td class="label">房间</td><td class="value">{room_name}</td></tr>
      <tr><td class="label">入住日期</td><td class="value">{order.check_in_date}</td></tr>
      <tr><td class="label">退房日期</td><td class="value">{order.check_out_date}</td></tr>
      {f'<tr><td class="label">物业地址</td><td class="value">{address}</td></tr>' if address else ''}
    </table>
  </div>
  <div class="footer">
    <p>如有问题请联系前台 | 祝您入住愉快</p>
  </div>
</div>
<div class="no-print" style="text-align:center;margin-top:24px">
  <button onclick="window.print()" style="padding:10px 32px;font-size:15px;background:#1677ff;color:#fff;border:none;border-radius:6px;cursor:pointer;">打印凭证</button>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)


# ─── Feature 10: Notification center ──────────────────────────────────────────

@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    db: DBSession,
    current_user: CurrentUser,
    is_read: Optional[bool] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """列出当前用户的通知"""
    q = select(Notification).where(Notification.user_id == current_user["user_id"])
    if is_read is not None:
        q = q.where(Notification.is_read == is_read)
    q = q.order_by(Notification.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/{notification_id}/read")
async def mark_as_read(
    notification_id: str,
    db: DBSession,
    current_user: CurrentUser,
):
    """标记通知为已读"""
    result = await db.execute(
        select(Notification).where(
            Notification.notification_id == notification_id,
            Notification.user_id == current_user["user_id"],
        )
    )
    notif = result.scalar_one_or_none()
    if not notif:
        raise HTTPException(status_code=404, detail="通知不存在")

    notif.is_read = True
    await db.commit()
    return {"message": "已标记为已读"}


@router.post("/read-all")
async def mark_all_as_read(
    db: DBSession,
    current_user: CurrentUser,
):
    """标记所有通知为已读"""
    await db.execute(
        update(Notification)
        .where(
            Notification.user_id == current_user["user_id"],
            Notification.is_read == False,
        )
        .values(is_read=True)
    )
    await db.commit()
    return {"message": "已全部标记为已读"}
