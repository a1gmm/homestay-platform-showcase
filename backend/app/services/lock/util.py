"""门锁 provider 共享小工具。"""
from __future__ import annotations

import secrets
import time
from typing import Callable


def gen_numeric_password(length: int = 6) -> str:
    """生成定长数字明文密码（含前导零）。manual 与 hxjiot 共用，避免重复。"""
    return f"{secrets.randbelow(10 ** length):0{length}d}"


def make_msgid_factory(clock: Callable[[], float] = time.time) -> Callable[[], int]:
    """返回单调递增的 msgId 生成器（秒级量级、< 2^31、同秒内也不重复）。

    现场差分实验证实慧享家按 32 位整数解析 msgId：13 位毫秒戳会溢出报「协议解释异常」，
    且重复 msgId 被拒。故用「epoch 秒 + 单调自增」：正常按秒推进（跨进程重启也唯一），
    同一秒多次调用则 +1 兜底防重复。秒级量级到 2038 年才触及 int32 上限。
    """
    last = 0

    def _next() -> int:
        nonlocal last
        candidate = int(clock())
        if candidate <= last:
            candidate = last + 1
        last = candidate
        return candidate

    return _next
