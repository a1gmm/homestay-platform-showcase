"""Shared datetime helpers — keep all CN-timezone logic in one place
so endpoints don't drift back to UTC date.today()."""
from datetime import date, datetime, timezone, timedelta

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
