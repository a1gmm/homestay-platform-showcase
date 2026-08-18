"""LockProvider 抽象协议（纯厂商层，见 plan §15.2）。

只懂「锁、码、有效期」，不懂订单/保洁——业务编排在 OrderLockService。
失败走返回值（三态 IssuedCode / bool），不抛异常：锁离线是常态，且多房批量
下码时一间失败不应中断其余。所有方法用厂商侧 vendor_room_id（base64），
系统 room_id ↔ vendor_room_id 的映射由编排层查 lock_devices 完成。
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.services.lock.types import IssuedCode, VendorKey, VendorRoom


@runtime_checkable
class LockProvider(Protocol):
    async def issue_code(
        self,
        vendor_room_id: str,
        phone: str,
        start_at: datetime,
        end_at: datetime,
        key_name: str,
        password: str | None = None,
    ) -> IssuedCode:
        """下一把码。password 为 None 时由实现生成（Hxjiot 含碰撞重试）。"""
        ...

    async def revoke_code(self, vendor_key_id: str) -> bool:
        """作废单把码（退房删客人码、保洁验收删保洁码）。"""
        ...

    async def revoke_all(self, vendor_room_id: str) -> bool:
        """清空整房所有授权。⚠️ 会连带清总码/保洁码，退房客人码请用 revoke_code。"""
        ...

    async def renew(self, vendor_room_id: str, phone: str, end_at: datetime) -> bool:
        """改期：原子改某手机号在某房的码有效期（不必先删后加）。"""
        ...

    async def list_rooms(self) -> list[VendorRoom]:
        """拉厂商房间列表，供 lock_devices 入库映射。"""
        ...

    async def list_keys(self, vendor_room_id: str) -> list[VendorKey]:
        """按 roomId 拉云端钥匙列表，供重推前旁证「码已下发」（云端视角，兜底 PULL）。"""
        ...

    async def set_webhook(self, callback_url: str) -> bool:
        """向厂商注册开锁事件回调 URL。"""
        ...
