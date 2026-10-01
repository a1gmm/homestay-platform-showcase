"""Typed, fail-closed authorization and emergency controls for monthly close.

Authentication establishes an identity only.  Callers must reload the current
database user and then evaluate one of the actions below against the exact
cycle/source/subject context.  System workers deliberately use a separate
identity type so a queue payload can never impersonate a user.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.monthly_close import MONTHLY_CLOSE_SOURCE_TYPES
from app.models.user import User, UserRole


class MonthlyCloseAction(StrEnum):
    view_cycle = "view_cycle"
    upload_source = "upload_source"
    confirm_mapping = "confirm_mapping"
    prepare_proposal = "prepare_proposal"
    approve_proposal = "approve_proposal"
    execute_proposal = "execute_proposal"
    finalize_cycle = "finalize_cycle"
    manage_intake_links = "manage_intake_links"
    permanently_delete_source = "permanently_delete_source"


class MonthlyCloseSystemAction(StrEnum):
    scan = "scan"
    notify = "notify"
    retry_failed_safe = "retry_failed_safe"
    classify_safe = "classify_safe"


class MonthlyCloseFeature(StrEnum):
    natural_language = "natural_language"
    external_intake = "external_intake"
    model_inference = "model_inference"
    source_adapters = "source_adapters"
    proposal_execution = "proposal_execution"
    low_risk_automation = "low_risk_automation"


@dataclass(frozen=True)
class MonthlyClosePermissionContext:
    cycle_id: str
    subject_cycle_id: str | None = None
    source_type: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    assigned_to: str | None = None
    uploaded_by: str | None = None
    owner_id: str | None = None
    low_role_upload_entrypoint: bool = False


@dataclass(frozen=True)
class MonthlyCloseSystemActor:
    actor_id: str
    verified: bool


class MonthlyClosePermissionDenied(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


_EMPLOYEE_ROLES = frozenset(
    {"admin", "finance", "operator", "cleaner", "keeper"}
)
_PREPARER_ACTIONS = frozenset(
    {
        MonthlyCloseAction.view_cycle,
        MonthlyCloseAction.upload_source,
        MonthlyCloseAction.confirm_mapping,
        MonthlyCloseAction.prepare_proposal,
    }
)
_LOW_ROLE_SOURCES: dict[str, frozenset[str]] = {
    "cleaner": frozenset({"cleaning_statement"}),
    "keeper": frozenset({"cleaning_statement", "linen_statement"}),
}
_SYSTEM_ALLOWLIST: dict[str, frozenset[MonthlyCloseSystemAction]] = {
    "monthly_close_monitor": frozenset(
        {MonthlyCloseSystemAction.scan, MonthlyCloseSystemAction.notify}
    ),
    "monthly_close_processor": frozenset(
        {
            MonthlyCloseSystemAction.classify_safe,
            MonthlyCloseSystemAction.retry_failed_safe,
        }
    ),
}
_UNSAFE_RETRY_STATES = frozenset(
    {
        "unknown",
        "succeeded_unverified",
        "failed_confirmed",
        "remediation_required",
    }
)


def _role(actor: User | dict[str, Any]) -> tuple[str, str, bool]:
    if isinstance(actor, User):
        role = actor.role.value if isinstance(actor.role, UserRole) else str(actor.role)
        return actor.user_id, role, bool(actor.is_active)
    if isinstance(actor, dict):
        role_value = actor.get("role")
        role = role_value.value if isinstance(role_value, UserRole) else str(role_value or "")
        return str(actor.get("user_id") or ""), role, bool(actor.get("is_active", True))
    raise MonthlyClosePermissionDenied(
        "monthly_close_user_actor_required",
        "该操作必须由当前有效员工账号执行",
    )


def assert_monthly_close_action_allowed(
    actor: User | dict[str, Any],
    action: MonthlyCloseAction,
    context: MonthlyClosePermissionContext,
) -> None:
    actor_id, role, active = _role(actor)
    if not actor_id or not active or role not in _EMPLOYEE_ROLES:
        raise MonthlyClosePermissionDenied(
            "monthly_close_actor_inactive", "账号已停用或无月结权限"
        )
    if not context.cycle_id:
        raise MonthlyClosePermissionDenied(
            "monthly_close_cycle_scope_required", "月结操作缺少月份范围"
        )
    if (
        context.subject_cycle_id is not None
        and context.subject_cycle_id != context.cycle_id
    ):
        raise MonthlyClosePermissionDenied(
            "monthly_close_subject_not_found", "对象不存在"
        )
    if context.source_type is not None and context.source_type not in MONTHLY_CLOSE_SOURCE_TYPES:
        raise MonthlyClosePermissionDenied(
            "monthly_close_source_scope_invalid", "月结资料范围无效"
        )
    if role == "admin":
        return
    if role in {"finance", "operator"}:
        if action in _PREPARER_ACTIONS:
            return
        raise MonthlyClosePermissionDenied(
            "monthly_close_action_forbidden", "当前岗位不能批准、执行或完成月结"
        )
    if action not in {
        MonthlyCloseAction.view_cycle,
        MonthlyCloseAction.upload_source,
    }:
        raise MonthlyClosePermissionDenied(
            "monthly_close_action_forbidden", "当前岗位只能查看和提交本人负责的资料"
        )
    allowed_sources = _LOW_ROLE_SOURCES[role]
    if context.source_type is not None and context.source_type not in allowed_sources:
        raise MonthlyClosePermissionDenied(
            "monthly_close_source_forbidden", "该资料不在当前岗位负责范围内"
        )
    if (
        action == MonthlyCloseAction.upload_source
        and context.source_type is None
        and not context.low_role_upload_entrypoint
    ):
        raise MonthlyClosePermissionDenied(
            "monthly_close_source_scope_required", "提交资料时必须指定资料类型"
        )
    if (
        action == MonthlyCloseAction.upload_source
        and not context.low_role_upload_entrypoint
    ):
        raise MonthlyClosePermissionDenied(
            "monthly_close_upload_entrypoint_forbidden",
            "请从员工月结入口提交本人负责的资料",
        )
    if context.subject_id is not None and actor_id not in {
        context.uploaded_by,
        context.assigned_to,
    }:
        raise MonthlyClosePermissionDenied(
            "monthly_close_subject_not_found", "对象不存在"
        )


async def authorize_current_monthly_close_actor(
    db: AsyncSession,
    authenticated: dict[str, Any],
    action: MonthlyCloseAction,
    context: MonthlyClosePermissionContext,
) -> User:
    """Reload the principal so token role/deactivation changes apply now."""
    actor_id = str(authenticated.get("user_id") or "")
    actor = (
        await db.scalar(
            select(User)
            .where(User.user_id == actor_id)
            .execution_options(populate_existing=True)
        )
        if actor_id
        else None
    )
    if actor is None:
        raise MonthlyClosePermissionDenied(
            "monthly_close_actor_inactive", "账号已停用或无月结权限"
        )
    assert_monthly_close_action_allowed(actor, action, context)
    return actor


def assert_monthly_close_system_action_allowed(
    actor: MonthlyCloseSystemActor,
    action: MonthlyCloseSystemAction,
    *,
    attempt_state: str | None = None,
    rollback_proven: bool = False,
) -> None:
    if not actor.verified or action not in _SYSTEM_ALLOWLIST.get(actor.actor_id, frozenset()):
        raise MonthlyClosePermissionDenied(
            "monthly_close_system_actor_forbidden", "系统任务身份未获授权"
        )
    if action == MonthlyCloseSystemAction.retry_failed_safe and (
        not rollback_proven
        or attempt_state != "failed_safe"
        or attempt_state in _UNSAFE_RETRY_STATES
    ):
        raise MonthlyClosePermissionDenied(
            "monthly_close_retry_requires_human",
            "该执行状态不能自动重试，请按恢复指引人工处理",
        )


_FEATURE_FLAGS: dict[MonthlyCloseFeature, tuple[str, str]] = {
    MonthlyCloseFeature.natural_language: (
        "MONTHLY_CLOSE_ASSISTANT_ENABLED",
        "自然语言助理已关闭，请使用月结清单和手工上传继续处理",
    ),
    MonthlyCloseFeature.external_intake: (
        "MONTHLY_CLOSE_EXTERNAL_INTAKE_ENABLED",
        "供应商收件链接已关闭，请由工作人员在月结中心手工上传资料",
    ),
    MonthlyCloseFeature.model_inference: (
        "MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED",
        "智能字段识别已关闭，请人工确认资料类型和列映射",
    ),
    MonthlyCloseFeature.source_adapters: (
        "MONTHLY_CLOSE_SOURCE_ADAPTERS_ENABLED",
        "自动对账方案暂时关闭，原件和人工映射仍可查看",
    ),
    MonthlyCloseFeature.proposal_execution: (
        "MONTHLY_CLOSE_PROPOSAL_EXECUTION_ENABLED",
        "方案执行已关闭，可继续准备和审批，恢复后再人工执行",
    ),
    MonthlyCloseFeature.low_risk_automation: (
        "MONTHLY_CLOSE_LOW_RISK_AUTOMATION_ENABLED",
        "自动提醒和安全重试已关闭，请人工查看月结清单",
    ),
}

_ADMIN_PREVIEW_FEATURES = frozenset(
    {
        MonthlyCloseFeature.natural_language,
        MonthlyCloseFeature.model_inference,
        MonthlyCloseFeature.source_adapters,
        MonthlyCloseFeature.proposal_execution,
    }
)


def assert_monthly_close_feature_enabled(
    feature: MonthlyCloseFeature,
    actor: User | dict[str, Any] | None = None,
) -> None:
    flag_name, message = _FEATURE_FLAGS[feature]
    if not bool(getattr(settings, flag_name, False)):
        raise MonthlyClosePermissionDenied(f"{feature.value}_disabled", message)
    if (
        actor is not None
        and feature in _ADMIN_PREVIEW_FEATURES
        and settings.MONTHLY_CLOSE_ASSISTANT_ADMIN_ONLY
    ):
        actor_id, role, active = _role(actor)
        if not actor_id or not active or role != "admin":
            raise MonthlyClosePermissionDenied(
                "monthly_close_admin_preview_only",
                "月结助理当前仅向管理员开放，请继续使用原月结入口",
            )
