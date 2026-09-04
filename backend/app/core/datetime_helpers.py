"""Shared datetime helpers — keep all CN-timezone logic in one place
so endpoints don't drift back to UTC date.today()."""
from datetime import date, datetime, time, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    CN_TZ = timezone(timedelta(hours=8))


def today_cn() -> date:
    """Today's date in Asia/Shanghai (CN business timezone)."""
    return datetime.now(CN_TZ).date()


def now_cn() -> datetime:
    """Current time in Asia/Shanghai (aware) —「北京当前时间」的单一来源。"""
    return datetime.now(CN_TZ)


def to_cn(dt: datetime | None) -> datetime | None:
    """Convert a stored datetime to Asia/Shanghai for display.

    Naive values are treated as UTC — SQLite and some drivers drop tzinfo."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CN_TZ)


def cn_date_range_utc(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    """中国业务日期区间转换为数据库 UTC 半开区间 ``[start, end)``。"""
    start = datetime.combine(date_from, time.min, tzinfo=CN_TZ).astimezone(timezone.utc)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=CN_TZ).astimezone(
        timezone.utc
    )
    return start, end
