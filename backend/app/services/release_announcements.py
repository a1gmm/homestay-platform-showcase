"""Account-scoped release announcement reads and acknowledgements."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.release_announcement import ReleaseAnnouncementReceipt
from app.models.user import UserRole
from app.release_announcements import (
    ReleaseAnnouncement,
    active_announcements_for_role,
)


class AnnouncementInvalidError(ValueError):
    """The acknowledgement list is not valid for the authenticated account."""


async def list_unread_announcements(
    db: AsyncSession,
    user_id: str,
    role: UserRole | str,
    now: datetime,
) -> tuple[ReleaseAnnouncement, ...]:
    candidates = active_announcements_for_role(role, now)
    if not candidates:
        return ()

    candidate_ids = tuple(item.announcement_id for item in candidates)
    acknowledged_ids = set(
        (
            await db.scalars(
                select(ReleaseAnnouncementReceipt.announcement_id).where(
                    ReleaseAnnouncementReceipt.user_id == user_id,
                    ReleaseAnnouncementReceipt.announcement_id.in_(candidate_ids),
                )
            )
        ).all()
    )
    return tuple(
        item for item in candidates if item.announcement_id not in acknowledged_ids
    )


async def acknowledge_announcements(
    db: AsyncSession,
    user_id: str,
    role: UserRole | str,
    announcement_ids: Sequence[str],
    now: datetime,
) -> tuple[str, ...]:
    requested_ids = tuple(announcement_ids)
    if not requested_ids or len(requested_ids) > 50:
        raise AnnouncementInvalidError
    if len(set(requested_ids)) != len(requested_ids):
        raise AnnouncementInvalidError

    active_by_id = {
        item.announcement_id: item
        for item in active_announcements_for_role(role, now)
    }
    if any(announcement_id not in active_by_id for announcement_id in requested_ids):
        raise AnnouncementInvalidError

    values = [
        {
            "user_id": user_id,
            "announcement_id": announcement_id,
            "acknowledged_at": now,
        }
        for announcement_id in requested_ids
    ]
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        statement = postgresql_insert(ReleaseAnnouncementReceipt).values(values)
    elif dialect_name == "sqlite":
        statement = sqlite_insert(ReleaseAnnouncementReceipt).values(values)
    else:
        raise RuntimeError("release announcements require PostgreSQL or SQLite")

    await db.execute(
        statement.on_conflict_do_nothing(
            index_elements=["user_id", "announcement_id"]
        )
    )
    return requested_ids
