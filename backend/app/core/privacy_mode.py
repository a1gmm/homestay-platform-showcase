"""Opt-in response redaction for administrator screen recordings.

The middleware changes only serialized JSON returned to a client that sends
``X-Privacy-Mode: 1``. ORM objects and database rows are never mutated.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.types import Message, Receive, Scope, Send


_logger = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])"
)
_ID_CARD_RE = re.compile(r"(?<!\d)\d{14}(\d{3}[0-9Xx])(?!\d)")
_IP_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")

_NAME_FIELDS = {
    "person_name",
    "guest_name",
    "owner_name",
    "customer_name",
    "contact_name",
    "display_name",
    "assignee_name",
    "cleaner_name",
    "keeper_name",
    "last_actor",
    "operator_name",
    "created_by_name",
}
_PHONE_FIELDS = {
    "phone",
    "guest_phone",
    "owner_phone",
    "contact_phone",
    "mobile",
    "telephone",
}
_IDENTIFIER_FIELDS = {
    "id_card",
    "id_number",
    "identity_number",
    "bank_account",
    "bank_card",
    "bank_card_number",
    "account_number",
}
_SECRET_FIELDS = {
    "access_token",
    "api_key",
    "credential",
    "password",
    "refresh_token",
    "secret",
    "token",
    "wifi_password",
    "wifi_name",
    "door_code",
    "lock_code",
    "otp",
    "verification_code",
    "wechat_openid",
    "openid",
}
_ACCOUNT_FIELDS = {"username", "wechat", "wechat_id", "bank_name"}
_ADDRESS_FIELDS = {
    "address",
    "province",
    "city",
    "district",
    "community_name",
    "building_no",
    "unit_no",
}
_PRIVATE_TEXT_FIELDS = {
    "notes",
    "extra_notes",
    "remark",
    "remarks",
    "comment",
    "private_text",
    "block_reason",
    "classification_reason",
    "deposit_withhold_reason",
    "exclusion_reason",
    "not_applicable_reason",
    "rejection_reason",
    "reopen_reason",
    "skipped_reason",
    "withhold_reason",
    "last_error",
    "submitted_label",
}
_FILE_NAME_FIELDS = {
    "filename",
    "original_name",
    "receipt_filename",
    "expense_filename",
    "source_filename",
    "source_sheet",
}
_PROPAGATED_TEXT_FIELDS = {
    "detail",
    "error",
    "error_message",
    "message",
    "messages",
    "summary",
    "text",
    "warnings",
}
_IP_FIELDS = {"ip", "ip_address", "client_ip"}
_PERSON_MARKER_FIELDS = {
    "guest_id",
    "owner_id",
    "customer_id",
    "lead_id",
    "phone",
    "id_number",
    "id_card",
    "bank_account",
    "username",
    "wechat",
}
_BUSINESS_STRING_FIELDS = {
    "billing_month",
    "channel",
    "date",
    "floor",
    "month",
    "nights",
    "room",
    "room_name",
    "status",
}
_BUSINESS_VALUE_SUFFIXES = (
    "_amount",
    "_at",
    "_cost",
    "_count",
    "_date",
    "_days",
    "_fee",
    "_month",
    "_nights",
    "_price",
    "_rate",
    "_revenue",
    "_status",
    "_total",
)


def _effective_field_name(container: dict[Any, Any], key: Any) -> str:
    field_name = str(key).lower()
    container_fields = {str(item).lower() for item in container}
    if field_name.endswith(("_phone", "_mobile", "_telephone")):
        return "phone"
    if field_name.endswith("_email"):
        return "email"
    if field_name in _IP_FIELDS:
        return field_name
    if field_name.endswith("_address"):
        return "address"
    if field_name in _FILE_NAME_FIELDS:
        return "private_text"
    if field_name == "name" and container_fields & _PERSON_MARKER_FIELDS:
        return "person_name"
    if field_name in {"title", "content"} and "notification_id" in container_fields:
        return "private_text"
    if field_name in {"title", "description"} and "task_id" in container_fields:
        return "private_text"
    if field_name == "description" and "expense_id" in container_fields:
        return "private_text"
    if field_name == "reason" and (
        "block_id" in container_fields
        or "sponsorship_adjustment_id" in container_fields
    ):
        return "private_text"
    if field_name == "label" and "link_id" in container_fields:
        return "private_text"
    if field_name == "tags" and "guest_id" in container_fields:
        return "private_text"
    if (
        field_name == "name"
        and {"row_count", "column_count", "rows"} <= container_fields
    ):
        return "private_text"
    return field_name


def _mask_name(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    return f"{value[0]}**"


def _mask_phone(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return stripped
    mainland = _PHONE_RE.sub(r"\1****\2", stripped)
    if mainland != stripped:
        return mainland
    digits = re.sub(r"\D", "", stripped)
    if len(digits) >= 4:
        return f"***-***-{digits[-4:]}"
    return "号码已隐藏"


def _mask_email(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return stripped
    match = _EMAIL_RE.fullmatch(stripped)
    if not match:
        return "邮箱已隐藏"
    return f"{match.group(1)[0]}***@{match.group(2)}"


def _mask_identifier(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return stripped
    if len(stripped) <= 4:
        return "****"
    return f"{'*' * (len(stripped) - 4)}{stripped[-4:]}"


def _mask_embedded_email(match: re.Match[str]) -> str:
    return f"{match.group(1)[0]}***@{match.group(2)}"


def _mask_for_field(field_name: str | None, value: str) -> str:
    if field_name in _NAME_FIELDS:
        return _mask_name(value)
    if field_name in _PHONE_FIELDS:
        return _mask_phone(value)
    if field_name in _IDENTIFIER_FIELDS:
        return _mask_identifier(value)
    if field_name == "email":
        return _mask_email(value)
    if field_name in _IP_FIELDS:
        return "***.***.***.***" if value else value
    if field_name in _ADDRESS_FIELDS:
        return "地址已隐藏" if value else value
    if field_name in _SECRET_FIELDS:
        return "******" if value else value
    if field_name in _ACCOUNT_FIELDS:
        return "账号已隐藏" if value else value
    if field_name in _PRIVATE_TEXT_FIELDS:
        return "内容已隐藏" if value else value
    return value


def _collect_replacements(value: Any, replacements: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            field_name = _effective_field_name(value, key)
            if isinstance(item, str):
                masked = _mask_for_field(field_name, item)
                if (
                    item.strip()
                    and masked != item
                    and field_name in (_NAME_FIELDS | _PHONE_FIELDS | {"email"})
                ):
                    replacements[item] = masked
            _collect_replacements(item, replacements)
    elif isinstance(value, list):
        for item in value:
            _collect_replacements(item, replacements)


def _redact(value: Any, replacements: dict[str, str], *, field_name: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact(item, replacements, field_name=_effective_field_name(value, key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, replacements, field_name=field_name) for item in value]
    if not isinstance(value, str):
        return value

    direct = _mask_for_field(field_name, value)
    if direct != value:
        return direct

    # Business identifiers are intentionally visible in demos so the operator
    # can explain a workflow. Numeric OTA/order IDs can look exactly like a
    # phone or identity card, so exempt ID fields before pattern redaction.
    if field_name:
        if (
            field_name == "id"
            or field_name.endswith("_id")
            or field_name.endswith("_ids")
        ):
            return value
        if (
            field_name in _BUSINESS_STRING_FIELDS
            or field_name.endswith(_BUSINESS_VALUE_SUFFIXES)
        ):
            return value

    masked = value
    if field_name in _PROPAGATED_TEXT_FIELDS or (
        field_name is not None
        and field_name.endswith(("_message", "_messages", "_text"))
    ):
        for raw, replacement in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            masked = masked.replace(raw, replacement)
    masked = _PHONE_RE.sub(r"\1****\2", masked)
    masked = _EMAIL_RE.sub(_mask_embedded_email, masked)
    masked = _ID_CARD_RE.sub(lambda match: f"**************{match.group(1)[-4:]}", masked)
    masked = _IP_RE.sub("***.***.***.***", masked)
    return masked


def redact_privacy_payload(value: Any) -> Any:
    """Return a redacted copy of a JSON-compatible value."""
    replacements: dict[str, str] = {}
    _collect_replacements(value, replacements)
    return _redact(value, replacements)


class PrivacyModeMiddleware:
    """Redact opted-in JSON GET responses without touching application state."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "GET":
            await self.app(scope, receive, send)
            return

        request_headers = dict(scope.get("headers", []))
        if request_headers.get(b"x-privacy-mode") != b"1":
            await self.app(scope, receive, send)
            return

        start_message: Message | None = None
        body_parts: list[bytes] = []
        blocked_non_json = False

        async def send_masked(message: Message) -> None:
            nonlocal blocked_non_json, start_message
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                content_type = headers.get(b"content-type", b"").lower()
                if b"application/json" not in content_type:
                    blocked_non_json = True
                    safe_body = json.dumps(
                        {"detail": "隐私演示模式禁止打开未脱敏的非 JSON 内容"},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    safe_headers = [
                        (key, val)
                        for key, val in message.get("headers", [])
                        if key.lower()
                        not in {
                            b"cache-control",
                            b"content-disposition",
                            b"content-encoding",
                            b"content-length",
                            b"content-md5",
                            b"content-type",
                            b"etag",
                        }
                    ]
                    safe_headers.extend(
                        [
                            (b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(safe_body)).encode("ascii")),
                            (b"cache-control", b"no-store"),
                            (b"vary", b"X-Privacy-Mode"),
                            (b"x-privacy-mode", b"blocked"),
                        ]
                    )
                    await send({**message, "status": 409, "headers": safe_headers})
                    await send(
                        {
                            "type": "http.response.body",
                            "body": safe_body,
                            "more_body": False,
                        }
                    )
                    return
                start_message = message
                return

            if blocked_non_json:
                return
            if message["type"] != "http.response.body" or start_message is None:
                await send(message)
                return

            body_parts.append(message.get("body", b""))
            if message.get("more_body", False):
                return

            raw_body = b"".join(body_parts)
            status = start_message.get("status", 200)
            try:
                payload = json.loads(raw_body)
                masked_body = json.dumps(
                    redact_privacy_payload(payload),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except Exception:
                # Privacy mode must fail closed: a malformed or otherwise
                # unredactable JSON response must never fall back to the raw
                # body while the UI claims that masking is active.
                _logger.exception("privacy-mode response redaction failed")
                status = 500
                masked_body = json.dumps(
                    {"detail": "隐私演示模式无法安全处理此响应"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")

            headers = [
                (key, val)
                for key, val in start_message.get("headers", [])
                if key.lower()
                not in {
                    b"cache-control",
                    b"content-encoding",
                    b"content-length",
                    b"content-md5",
                    b"etag",
                }
            ]
            headers.append((b"content-length", str(len(masked_body)).encode("ascii")))
            headers.append((b"cache-control", b"no-store"))
            headers.append((b"vary", b"X-Privacy-Mode"))
            headers.append((b"x-privacy-mode", b"masked"))
            await send({**start_message, "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": masked_body, "more_body": False})

        await self.app(scope, receive, send_masked)
