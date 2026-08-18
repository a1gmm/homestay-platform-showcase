"""门锁 provider 层的值对象（见 plan §15.1）。

下码非实时（网关有几秒延迟、锁可能离线），所以结果是三态而非「成功/异常」：
  GRANTED  码已确认下到锁
  QUEUED   已受理但未确认到锁（锁离线待 Celery 重试 / manual 待前台手动设置）
  FAILED   真失败，需人工介入
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class IssueOutcome(str, Enum):
    GRANTED = "granted"
    QUEUED = "queued"
    FAILED = "failed"


@dataclass(frozen=True)
class IssuedCode:
    """一次下码的结果。vendor_key_id 由调用方（OrderLockService）存进 door_codes，
    作废/改期时回引用。"""

    outcome: IssueOutcome
    password: str | None = None          # 明文密码（发给客人/保洁）
    vendor_key_id: str | None = None     # 厂商 keyId（base64 字符串），作废/改期要用
    vendor_lock_key_id: int | None = None
    vendor_key_group_id: int | None = None
    error: str | None = None             # 人类可读失败原因（日志/告警，禁直显客人）


@dataclass(frozen=True)
class VendorRoom:
    """list_rooms 返回项，供 lock_devices 入库映射（厂商 roomId ↔ 系统 room_id）。"""

    vendor_room_id: str                  # base64 字符串，厂商侧主键
    name: str
    online: bool
    battery: int | None = None
    lock_mac: str | None = None
    room_state: int | None = None        # 慧享家 roomState：1闲置 2出租/在住 4到期 5打扫 6维修（房态对账用）


# 慧享家 keyState（apartmentKeyList 返回）：1待生效 3正常 4冻结 5失效/删除 6待删除
# 7待冻结 8待激活 9待修改。3=正常=云端认为码已下发生效（重推前云端确认双保险用）。
KEY_STATE_NORMAL = 3


@dataclass(frozen=True)
class VendorKey:
    """list_keys（apartmentKeyList）返回项：云端记录的一把钥匙。

    云端视角（非物理锁真值），key_state==3 表示云端认为已下发生效。用于重推前旁证
    「码其实已在锁上」→ 确认不再瞎重推（回调确认 logType=8 是首选，这是兜底 PULL）。
    """

    phone_no: str | None
    key_state: int | None
    end_at: datetime | None = None
    vendor_key_id: str | None = None
