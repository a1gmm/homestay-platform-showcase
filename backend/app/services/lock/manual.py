"""ManualProvider —— 兜底 noop provider（见 plan §8.1 / §15.2）。

LOCK_PROVIDER=manual 时启用：不接任何厂商，退回人工模式（前台在慧享家 App
手动设码）。也是慧享家维护/故障时的一键回滚目标，以及编排层测试的真实替身。

issue_code 仍生成一个 6 位明文密码交给前台手动设置，outcome=QUEUED 表示
「已受理但未自动确认到锁」。编排层在 manual 模式下据此把 door_code 记为待人工，
不进 Celery 重试。其余操作均为安全 noop。
"""
from __future__ import annotations

from datetime import datetime

from app.services.lock.types import IssuedCode, IssueOutcome, VendorKey, VendorRoom
from app.services.lock.util import gen_numeric_password


class ManualProvider:
    """实现 LockProvider 协议的兜底实现。"""

    async def issue_code(
        self,
        vendor_room_id: str,
        phone: str,
        start_at: datetime,
        end_at: datetime,
        key_name: str,
        password: str | None = None,
    ) -> IssuedCode:
        return IssuedCode(
            outcome=IssueOutcome.QUEUED,
            password=password or gen_numeric_password(),
            vendor_key_id=None,
        )

    async def revoke_code(self, vendor_key_id: str) -> bool:
        return True

    async def revoke_all(self, vendor_room_id: str) -> bool:
        return True

    async def renew(self, vendor_room_id: str, phone: str, end_at: datetime) -> bool:
        return True

    async def list_rooms(self) -> list[VendorRoom]:
        return []

    async def list_keys(self, vendor_room_id: str) -> list[VendorKey]:
        return []

    async def set_webhook(self, callback_url: str) -> bool:
        return True
