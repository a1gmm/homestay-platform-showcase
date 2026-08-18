"""相对/绝对周期 token → 确定日期区间（含端点）。

设计铁律：**LLM 不算日期**，只吐周期 token，日期由本模块用 `today_cn` 单一时钟解析。
解析不了的含糊词（「最近」「这几天」）抛 `PeriodError`，交上层走回显/拒答，
绝不硬套一个月份吐错数字（铁律 #2）。
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from app.core.datetime_helpers import today_cn


class PeriodError(ValueError):
    """周期 token 无法解析成确定区间（含糊/非法）。上层据此走回显或拒答。"""


_MONTH_TOKEN = re.compile(r"^(\d{4})-(\d{1,2})$")
_BARE_MONTH = re.compile(r"^(\d{1,2})$")


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    if not 1 <= month <= 12:
        raise PeriodError(f"月份非法：{month}")
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def resolve_period(token: str | None, today: date | None = None) -> tuple[date, date, str]:
    """把周期 token 解析为 (start_date, end_date, 中文label)。

    支持：today / yesterday / this_week / last_week / this_month / last_month /
    this_year / last_year；绝对月份 "2026-06"；裸月份 "6"（按当年）。
    其它一律 PeriodError。
    """
    if today is None:
        today = today_cn()
    if not token or not isinstance(token, str):
        raise PeriodError(f"空/非法周期：{token!r}")
    t = token.strip().lower()

    if t == "today":
        return today, today, f"{today.month}月{today.day}日"
    if t == "yesterday":
        y = today - timedelta(days=1)
        return y, y, f"{y.month}月{y.day}日"
    if t == "this_week":
        start = today - timedelta(days=today.weekday())  # 周一
        end = start + timedelta(days=6)                   # 周日
        return start, end, "本周"
    if t == "last_week":
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        end = start + timedelta(days=6)
        return start, end, "上周"
    if t == "this_month":
        s, e = _month_bounds(today.year, today.month)
        return s, e, f"{today.month}月"
    if t == "last_month":
        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        s, e = _month_bounds(year, month)
        return s, e, f"{month}月"
    if t == "this_year":
        return date(today.year, 1, 1), date(today.year, 12, 31), f"{today.year}年"
    if t == "last_year":
        y = today.year - 1
        return date(y, 1, 1), date(y, 12, 31), f"{y}年"

    m = _MONTH_TOKEN.match(t)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        s, e = _month_bounds(year, month)
        return s, e, f"{month}月"

    m = _BARE_MONTH.match(t)
    if m:
        month = int(m.group(1))
        s, e = _month_bounds(today.year, month)
        return s, e, f"{month}月"

    raise PeriodError(f"无法解析周期：{token!r}")
