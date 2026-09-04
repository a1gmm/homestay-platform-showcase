"""Code-owned, role-scoped release announcements."""
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from urllib.parse import urlsplit

from app.models.user import UserRole


INTERNAL_RELEASE_ANNOUNCEMENT_ROLES = frozenset(
    {
        UserRole.admin,
        UserRole.operator,
        UserRole.finance,
        UserRole.keeper,
        UserRole.cleaner,
    }
)


@dataclass(frozen=True, slots=True)
class ReleaseAnnouncement:
    announcement_id: str
    title: str
    summary: str
    items: tuple[str, ...]
    published_at: datetime
    roles: frozenset[UserRole]
    active: bool = True
    cta_label: str | None = None
    cta_path: str | None = None


def validate_release_announcements(
    items: tuple[ReleaseAnnouncement, ...],
) -> dict[str, ReleaseAnnouncement]:
    """Validate code-owned announcements before the application serves traffic."""
    by_id: dict[str, ReleaseAnnouncement] = {}
    for item in items:
        if (
            not item.announcement_id.strip()
            or item.announcement_id != item.announcement_id.strip()
            or item.announcement_id in by_id
        ):
            raise ValueError("release announcement IDs must be non-empty and unique")
        if item.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        if (
            not item.title.strip()
            or item.title != item.title.strip()
            or not item.summary.strip()
            or item.summary != item.summary.strip()
            or not item.items
        ):
            raise ValueError("release announcement copy must be non-empty")
        if any(not line.strip() or line != line.strip() for line in item.items):
            raise ValueError("release announcement items must be non-empty")
        if (
            not item.roles
            or any(not isinstance(role, UserRole) for role in item.roles)
            or not item.roles.issubset(INTERNAL_RELEASE_ANNOUNCEMENT_ROLES)
        ):
            raise ValueError("release announcement roles are invalid")
        if item.cta_path is not None:
            if any(ord(char) <= 0x1F or ord(char) == 0x7F for char in item.cta_path):
                raise ValueError("release announcement CTA must be an internal path")
            parsed_cta_path = urlsplit(item.cta_path)
            if (
                not item.cta_path.startswith("/")
                or item.cta_path.startswith("//")
                or "\\" in item.cta_path
                or parsed_cta_path.scheme
                or parsed_cta_path.netloc
            ):
                raise ValueError("release announcement CTA must be an internal path")
        by_id[item.announcement_id] = item
    return by_id


RELEASE_ANNOUNCEMENTS: tuple[ReleaseAnnouncement, ...] = (
    ReleaseAnnouncement(
        announcement_id="2026-08-22-any-ota-billing-recon",
        title="账单对账现已支持更多 OTA 格式",
        summary="现在可上传更多 OTA Excel 账单，并在对账前确认智能识别结果。",
        items=(
            "支持携程、美团、去哪儿、同程、飞猪及其他 Excel 账单",
            "上传后自动识别订单号、客人、日期和金额列",
            "对账前可以检查并修改识别结果",
            "修改字段后会重新分析，确认无误才生成差异",
            "已确认过的表格格式，下次可以自动复用",
        ),
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
        roles=frozenset({UserRole.admin, UserRole.operator, UserRole.finance}),
        cta_label="财务 → 账单对账",
        cta_path="/finance/billing-recon",
    ),
)

RELEASE_ANNOUNCEMENTS_BY_ID = validate_release_announcements(RELEASE_ANNOUNCEMENTS)


def active_announcements_for_role(
    role: UserRole | str,
    now: datetime | None = None,
) -> tuple[ReleaseAnnouncement, ...]:
    resolved_role = UserRole(role)
    resolved_now = now or datetime.now(timezone.utc)
    if resolved_now.tzinfo is None:
        resolved_now = resolved_now.replace(tzinfo=UTC)
    else:
        resolved_now = resolved_now.astimezone(UTC)

    return tuple(
        sorted(
            (
                item
                for item in RELEASE_ANNOUNCEMENTS
                if item.active
                and item.published_at <= resolved_now
                and resolved_role in item.roles
            ),
            key=lambda item: (item.published_at, item.announcement_id),
        )
    )
