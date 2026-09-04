from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime
from functools import cache
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.miniapp_content import (
    MiniappContentWorkspace,
    MiniappMedia,
    MiniappStructuredDraft,
)
from app.schemas.miniapp_content_guides import (
    StayGuideDraft,
    TravelGuideDraft,
)
from app.schemas.miniapp_content_guides_api import (
    GuideChannel,
    GuideDraft,
    StructuredDraftRevision,
)
from app.services.audit import log_action_tx
from app.services.miniapp_content.errors import (
    ContentValidationError,
    DraftConflictError,
    PublicationPolicyIssue,
)


_DRAFT_MODELS: dict[str, type[StayGuideDraft] | type[TravelGuideDraft]] = {
    "stay_guide": StayGuideDraft,
    "travel": TravelGuideDraft,
}
_DRAFT_USABLE_MEDIA_STATES = {"draft", "published"}

_UNICODE_DASHES = str.maketrans(
    {character: "-" for character in "‐‑‒–—―−﹘﹣－"}
)
_UNICODE_SLASHES = str.maketrans({"∕": "/", "⁄": "/", "⧸": "/"})
_MOBILE_PHONE = re.compile(
    r"(?<!\d)(?:(?:\+|00)86[\s.\-]?)?1[3-9](?:[\s.\-]?\d){9}(?!\d)"
)
_LABELED_LANDLINE = re.compile(
    r"(?:联系电话|联系号码|客服电话|服务热线|客服热线|电话|热线)"
    r"\s*(?:[:=为是]\s*)?(?<!\d)0\d{2,3}(?:[\s.\-]?\d){7,8}(?!\d)"
)
_LANDLINE = re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")
_IDENTITY_NUMBER = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")
_VALUE_TERMINATORS = r",，。;；!?！？"
_VALUE_TERMINATOR = re.compile(f"[{_VALUE_TERMINATORS}]")
_SAFE_CREDENTIAL_PLACEHOLDER = re.compile(
    r"(?:"
    r"暂无|无(?:需|须)?(?:密码|口令)?|未(?:设置|提供|开放)|不(?:提供|公开)|"
    r"not\s+(?:set|provided|available|published)|"
    r"no\s+(?:password|passcode|pin)"
    r")",
    re.IGNORECASE,
)
_SAFE_CREDENTIAL_INSTRUCTION = re.compile(
    r"(?:"
    r"请?(?:联系|咨询|询问)(?:管家|前台|工作人员)(?:获取)?|"
    r"请?查看(?:订单|小程序)(?:页面)?|"
    r"请?(?:扫描(?:房内)?二维码|扫码)|"
    r"请?到店(?:后)?请?询问(?:管家|前台|工作人员)?|"
    r"以(?:现场(?:管家)?|订单(?:页面)?|小程序(?:页面)?|管家|前台|工作人员)"
    r"(?:告知|显示|通知|提供|指引)?为准|"
    r"(?:please\s+)?(?:contact|ask)\s+(?:the\s+)?"
    r"(?:host|front\s+desk|staff)|"
    r"(?:please\s+)?check\s+(?:the\s+)?(?:app|booking)"
    r")",
    re.IGNORECASE,
)
_WIFI_CREDENTIAL = re.compile(
    rf"(?:"
    r"(?<![A-Za-z])(?:wi-fi|wlan|wireless(?:\s+network)?)(?![A-Za-z])|"
    r"无线网|网络"
    rf").{{0,12}}?"
    r"(?:密码|口令|(?<![A-Za-z])(?:password|passcode|pin)(?![A-Za-z]))\s*"
    r"(?:[:=为是]\s*)?",
    re.IGNORECASE,
)
_ROOM_NUMBER = re.compile(
    r"(?:房间号|客房号|房号)\s*(?:[:=为是]\s*)?"
    r"[A-Za-z]?\d[A-Za-z0-9\-]{0,11}(?![A-Za-z0-9])"
)
_PRIVATE_DOOR_NUMBER = re.compile(
    r"(?:门牌号|门牌)\s*(?:[:=为是]\s*)?\d{1,6}(?!\d)"
    r".{0,20}(?:直接入住|办理入住|入住|开门|进门|房门|门锁|门禁)"
)
_ACCESS_QR = re.compile(
    r"(?:门锁|门禁|开门|房门|房间门)(?:密码|口令|码|凭证)?"
    r".{0,12}(?:扫(?:描|码)?\s*)?二维码|"
    r"二维码.{0,12}(?:门锁|门禁|开门|房门|房间门)"
)
_ACCESS_CREDENTIAL = re.compile(
    r"(?:"
    r"门锁|门禁|开门|房间门|房门|房间|"
    r"(?<![A-Za-z])(?:door|room|access)(?![A-Za-z])"
    r")\s*"
    r"(?:密码|口令|码|(?<![A-Za-z])(?:password|pin|passcode)(?![A-Za-z]))"
    r"\s*(?:[:=为是]\s*)?",
    re.IGNORECASE,
)
_LABELED_SECRET = re.compile(
    r"(?:密码|口令)\s*[:=为是]\s*[A-Za-z0-9][A-Za-z0-9._@#*+\-]{3,}"
)
_GUEST_VALUE = re.compile(
    r"(?:"
    r"(?:入住人|住客|客人)\s*(?:的\s*)?(?P<identity_label>姓名|名字)"
    r"(?:\s*\(必填\))?\s*(?:[:=为是,\-/、]\s*)?|"
    r"(?P<english_identity_label>"
    r"(?<![A-Za-z])guest(?![A-Za-z])\s+name"
    r")\s*(?:[:=/\-]\s*)?|"
    r"(?:入住人|住客|客人)(?:\s*[:=为是,\-/、]\s*|\s+|(?=[(\[【“\"「『]))|"
    r"(?<![A-Za-z])guest(?![A-Za-z])"
    r"(?:\s*[:=/\-]\s*|\s*(?=[「『]))"
    r")",
    re.IGNORECASE,
)
_EXPLICIT_GUEST_LABEL = re.compile(
    r"(?<![A-Za-z])guest(?![A-Za-z])|入住人|住客|客人",
    re.IGNORECASE,
)
_GUEST_LABEL_BOUNDARY_CONTROLS = frozenset(":=为是,/-、「『")
_SAFE_GUEST_INSTRUCTION = re.compile(
    r"请?(?:在|于)(?:订单|小程序)(?:页面)?(?:内)?填写"
)
_SAFE_GUEST_PLACEHOLDER = re.compile(
    r"(?:"
    r"暂无|无|未填|未填写|未提供|待填写|不适用|"
    r"not\s+(?:provided|available|applicable|set)|to\s+be\s+completed"
    r")",
    re.IGNORECASE,
)
_SAFE_GUEST_AUDIENCE = re.compile(
    r"(?:"
    r"亲子家庭|家庭|亲子|情侣|朋友|商务(?:旅客|客人)?|团队|家庭出游|朋友出游|"
    r"family|families|friends|couples|groups|business\s+travelers?|"
    r"families\s+and\s+friends"
    r")",
    re.IGNORECASE,
)
_SAFE_GUEST_COUNT = re.compile(
    r"(?:"
    r"\d{1,2}\s*(?:位\s*)?(?:住客|客人|成人|儿童|人)|"
    r"\d{1,2}\s+(?:guests?|adults?|children|people|persons?)"
    r")",
    re.IGNORECASE,
)
_SAFE_GENERIC_GUEST_VALUE = re.compile(r"我们的朋友")
_MAX_GUEST_NAME_PREFIX_CHARS = 40
_GUEST_LEADING_ANNOTATION = re.compile(r"^\(\s*本人\s*\)\s*")
_GUEST_STRUCTURAL_WRAPPER_PAIRS = {
    "【": "】",
    "(": ")",
    "[": "]",
    # NFKC maps FE41-FE44 and FF62/FF63 into these canonical pairs.
    "「": "」",
    "『": "』",
}
_GUEST_STRUCTURAL_WRAPPER_CONTROLS = frozenset(
    (
        *_GUEST_STRUCTURAL_WRAPPER_PAIRS,
        *_GUEST_STRUCTURAL_WRAPPER_PAIRS.values(),
    )
)
_ASCII_GUEST_QUOTATION_MARKS = frozenset("'\"`")
_MAX_GUEST_NAME_NORMALIZATION_STEPS = _MAX_GUEST_NAME_PREFIX_CHARS
_UNLABELED_SAFE_INSTRUCTION_VALUE = re.compile(
    r"(?:"
    r"入住人请?(?:在|于)(?:订单|小程序)(?:页面)?(?:内)?填写|"
    r"钥匙请?交给(?:管家|前台|工作人员)保管"
    r")"
)
_PRIVATE_KEY_ACCESS = re.compile(
    r"钥匙\s*(?:放在|藏在|位于|在).{1,30}"
    r"(?:直接|自行)?(?:取用|拿取|取出|拿出).{0,6}(?:入住|开门|进门)"
)
_PRIVATE_QR = re.compile(
    r"(?:微信|客服|管家|联系|支付|付款).{0,10}二维码|"
    r"二维码.{0,10}(?:微信|客服|管家|联系|支付|付款)"
)
_PRIVATE_ACCOUNT = re.compile(
    r"(?:微信号|客服号)\s*[:=为是]\s*[A-Za-z][A-Za-z0-9_\-]{5,19}"
)

# These patterns run only against a mark-free shadow copy. Their semantic
# windows are the same bounded label families used by the publication policy;
# matched marks are removed only from those local windows before whole-text
# NFKC. Ordinary prose is never globally stripped of combining marks.
_PRE_NFKC_GUEST_LABEL = re.compile(
    r"(?:"
    r"(?<![A-Za-z])guest(?![A-Za-z])"
    r"(?:\s+name(?=\s*[:：=＝/\-－「『])|(?=\s*[:：=＝/\-－「『]))|"
    r"(?:入住人|住客|客人)(?:\s*的\s*)?(?:姓名|名字)?"
    r"(?=\s*(?:[:：=＝为是,，/\-－、「『(\[【“\"]|\s+))"
    r")",
    re.IGNORECASE,
)
_PRE_NFKC_PRIVACY_LABELS = (
    _PRE_NFKC_GUEST_LABEL,
    _WIFI_CREDENTIAL,
    _ACCESS_CREDENTIAL,
)


def _independently_classified_value_match(text: str) -> re.Match[str] | None:
    for pattern in (_WIFI_CREDENTIAL, _ACCESS_CREDENTIAL, _GUEST_VALUE):
        match = pattern.match(text)
        if match:
            return match
    return _UNLABELED_SAFE_INSTRUCTION_VALUE.fullmatch(text)


def _is_complete_classified_candidate(text: str) -> bool:
    match = _independently_classified_value_match(text)
    return bool(match and not _VALUE_TERMINATOR.search(text, match.end()))


def _is_complete_classified_chain(text: str) -> bool:
    @cache
    def classify(candidate: str) -> bool:
        candidate = candidate.strip()
        while True:
            normalized = candidate.lstrip(_VALUE_TERMINATORS).lstrip()
            if normalized == candidate:
                break
            candidate = normalized
        if not candidate:
            return True
        for punctuation in _VALUE_TERMINATOR.finditer(candidate):
            value = candidate[: punctuation.start()].strip()
            remainder = candidate[punctuation.end() :].strip()
            if value and _is_complete_classified_candidate(value) and classify(
                remainder
            ):
                return True
        return _is_complete_classified_candidate(candidate)

    return classify(text)


def _logical_value_end(text: str, start: int) -> int:
    for punctuation in _VALUE_TERMINATOR.finditer(text[start:]):
        end = start + punctuation.start()
        remainder = text[start + punctuation.end() :].lstrip()
        if not remainder or _is_complete_classified_chain(remainder):
            return end
    return len(text)


def _labelled_values(
    text: str, label: re.Pattern[str]
) -> Iterable[tuple[re.Match[str], str, int]]:
    for match in label.finditer(text):
        end = _logical_value_end(text, match.end())
        yield match, text[match.end() : end].strip(), end


def _is_safe_credential_value(value: str) -> bool:
    return bool(
        _SAFE_CREDENTIAL_PLACEHOLDER.fullmatch(value)
        or _SAFE_CREDENTIAL_INSTRUCTION.fullmatch(value)
    )


def _has_unsafe_credential_value(text: str, label: re.Pattern[str]) -> bool:
    for _match, value, _end in _labelled_values(text, label):
        if value and not _is_safe_credential_value(value):
            return True
    return False


def _has_unsafe_wifi_credential_value(text: str) -> bool:
    return _has_unsafe_credential_value(text, _WIFI_CREDENTIAL)


def _has_unsafe_access_reference(text: str) -> bool:
    safe_spans: list[tuple[int, int]] = []
    for match, value, end in _labelled_values(text, _ACCESS_CREDENTIAL):
        if value and not _is_safe_credential_value(value):
            return True
        if value:
            safe_spans.append((match.start(), end))
    return any(
        not any(
            start <= match.start() and match.end() <= end
            for start, end in safe_spans
        )
        for match in _ACCESS_QR.finditer(text)
    )


def _is_guest_quotation_mark(character: str) -> bool:
    category = unicodedata.category(character)
    return bool(
        character in _ASCII_GUEST_QUOTATION_MARKS
        or category in {"Pi", "Pf"}
        or "QUOTATION MARK" in unicodedata.name(character, "")
    )


def _is_guest_label_boundary_control(character: str) -> bool:
    normalized = (
        unicodedata.normalize("NFKC", character)
        .translate(_UNICODE_DASHES)
        .translate(_UNICODE_SLASHES)
    )
    return len(normalized) == 1 and normalized in _GUEST_LABEL_BOUNDARY_CONTROLS


def _normalize_privacy_label_marks_before_nfkc(text: str) -> str:
    shadow: list[str] = []
    mark_gaps: list[tuple[int, int]] = []
    for source_index, character in enumerate(text):
        if unicodedata.category(character).startswith("M"):
            mark_gaps.append((source_index, len(shadow)))
            continue
        shadow.append(character)
    if not mark_gaps:
        return text

    shadow_text = "".join(shadow)
    local_mark_positions: set[int] = set()
    for label in _PRE_NFKC_PRIVACY_LABELS:
        for match in label.finditer(shadow_text):
            local_mark_positions.update(
                source_index
                for source_index, gap in mark_gaps
                if match.start() < gap <= match.end()
            )
    if not local_mark_positions:
        return text
    return "".join(
        character
        for source_index, character in enumerate(text)
        if source_index not in local_mark_positions
    )


def _normalize_guest_label_boundary_marks(text: str) -> tuple[str, bool]:
    characters = list(text)
    changed = False
    for label in _EXPLICIT_GUEST_LABEL.finditer(text):
        index = label.end()
        mark_positions: list[int] = []
        over_limit = False
        while index < len(text):
            character = text[index]
            if character.isspace():
                index += 1
                continue
            if not unicodedata.category(character).startswith("M"):
                break
            if len(mark_positions) < _MAX_GUEST_NAME_PREFIX_CHARS:
                mark_positions.append(index)
            else:
                over_limit = True
            index += 1
        if (
            mark_positions
            and index < len(text)
            and _is_guest_label_boundary_control(text[index])
        ):
            if over_limit:
                return text, True
            for position in mark_positions:
                characters[position] = " "
            changed = True
    return ("".join(characters) if changed else text), False


def _normalize_guest_name_candidate(value: str) -> str | None:
    candidate = value.strip()
    for _step in range(_MAX_GUEST_NAME_NORMALIZATION_STEPS):
        annotation = _GUEST_LEADING_ANNOTATION.match(candidate)
        if annotation:
            candidate = candidate[annotation.end() :].lstrip()
            continue
        if not candidate:
            return candidate
        opener = candidate[0]
        if unicodedata.category(opener).startswith("M"):
            candidate = candidate[1:].lstrip()
            continue
        if _is_guest_quotation_mark(opener):
            candidate = candidate[1:].lstrip()
            if candidate and _is_guest_quotation_mark(candidate[-1]):
                candidate = candidate[:-1].rstrip()
            continue
        if opener not in _GUEST_STRUCTURAL_WRAPPER_CONTROLS:
            if unicodedata.category(opener) == "Ps":
                return None
            return candidate
        closer = _GUEST_STRUCTURAL_WRAPPER_PAIRS.get(opener)
        closer_index = candidate.find(closer, 1) if closer else -1
        if closer_index < 0:
            candidate = candidate[1:].lstrip()
            continue
        candidate = (
            candidate[1:closer_index] + candidate[closer_index + 1 :]
        ).strip()
    return None


def _has_bounded_unicode_guest_name_prefix(value: str) -> bool:
    candidate = value.lstrip()
    # Complete safe values are handled before this point. For any remaining
    # explicit guest value, bounded decorative punctuation, symbols, emoji,
    # numbers, whitespace, and marks cannot hide a following name.
    decoration_count = 0
    for character in candidate:
        category = unicodedata.category(character)
        if category.startswith("L"):
            return True
        if character.isspace() or category[0] in {"M", "N", "P", "S"}:
            decoration_count += 1
            if decoration_count > _MAX_GUEST_NAME_PREFIX_CHARS:
                return True
            continue
        return False
    return False


def _is_safe_guest_value(value: str) -> bool:
    return bool(
        _SAFE_GUEST_PLACEHOLDER.fullmatch(value)
        or _SAFE_GUEST_INSTRUCTION.fullmatch(value)
        or _SAFE_GUEST_AUDIENCE.fullmatch(value)
        or _SAFE_GUEST_COUNT.fullmatch(value)
        or _SAFE_GENERIC_GUEST_VALUE.fullmatch(value)
    )


def _has_guest_identity_value(text: str) -> bool:
    match_text, boundary_exhausted = _normalize_guest_label_boundary_marks(text)
    if boundary_exhausted:
        return True
    for match, value, _end in _labelled_values(match_text, _GUEST_VALUE):
        if not value or _SAFE_GUEST_PLACEHOLDER.fullmatch(value):
            continue
        if match.group("identity_label") or match.group("english_identity_label"):
            return True
        if _is_safe_guest_value(value):
            continue
        name_candidate = _normalize_guest_name_candidate(value)
        if name_candidate is None:
            return True
        if _is_safe_guest_value(name_candidate):
            continue
        if _has_bounded_unicode_guest_name_prefix(name_candidate):
            return True
    return False


_PRIVACY_TEXT_POLICIES = (
    (_MOBILE_PHONE.search, "疑似公开手机号，请改用小程序内的统一联系入口"),
    (_LANDLINE.search, "疑似公开固定电话，请改用小程序内的统一联系入口"),
    (_LABELED_LANDLINE.search, "疑似公开固定电话，请改用小程序内的统一联系入口"),
    (_IDENTITY_NUMBER.search, "疑似身份证或证件号码，请删除个人身份信息"),
    (_has_guest_identity_value, "疑似公开住客姓名，请删除客人身份信息"),
    (
        _has_unsafe_wifi_credential_value,
        "疑似 Wi-Fi 密码，请只保留不含凭证的通用设备说明",
    ),
    (_ROOM_NUMBER.search, "疑似门锁、门禁或房间凭证，请删除具体号码或密码"),
    (_PRIVATE_DOOR_NUMBER.search, "疑似门锁、门禁或房间凭证，请删除具体号码或密码"),
    (
        _has_unsafe_access_reference,
        "疑似门锁、门禁或房间凭证，请删除具体号码或密码",
    ),
    (_PRIVATE_KEY_ACCESS.search, "疑似门锁、门禁或房间凭证，请删除具体号码或密码"),
    (_LABELED_SECRET.search, "疑似公开密码或口令，请删除具体凭证"),
    (_PRIVATE_QR.search, "疑似私人联系或支付二维码，请改用小程序内的统一入口"),
    (_PRIVATE_ACCOUNT.search, "疑似私人联系账号，请改用小程序内的统一联系入口"),
)


def _normalize_public_text(value: str) -> str:
    value = _normalize_privacy_label_marks_before_nfkc(value)
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if unicodedata.category(character) != "Cf"
    ).translate(_UNICODE_DASHES).translate(_UNICODE_SLASHES)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return re.sub(
        r"(?i)(?<![A-Za-z])wi\s*[-_.]?\s*fi(?![A-Za-z])",
        "wi-fi",
        normalized,
    )


def _visible_guide_text(
    draft: StayGuideDraft | TravelGuideDraft,
) -> Iterable[tuple[str, str]]:
    yield "/title", draft.title
    yield "/intro", draft.intro
    if isinstance(draft, StayGuideDraft):
        sections = draft.sections
        for name in (
            "arrivalDeparture",
            "parking",
            "wifiAndDevices",
            "houseRules",
            "checkOut",
            "support",
            "faq",
        ):
            section = getattr(sections, name)
            if not section.visible:
                continue
            base = f"/sections/{name}"
            yield f"{base}/summary", section.summary
            if section.details is not None:
                yield f"{base}/details", section.details
            if name == "faq":
                for index, item in enumerate(section.items):
                    yield f"{base}/items/{index}/question", item.question
                    yield f"{base}/items/{index}/answer", item.answer
        return

    for index, item in enumerate(draft.recommendations):
        if not item.visible:
            continue
        base = f"/recommendations/{index}"
        yield f"{base}/name", item.name
        yield f"{base}/reason", item.reason
        if item.approximateLocation is not None:
            yield f"{base}/approximateLocation", item.approximateLocation
        if item.suggestedDuration is not None:
            yield f"{base}/suggestedDuration", item.suggestedDuration
        for audience_index, audience in enumerate(item.audiences):
            yield f"{base}/audiences/{audience_index}", audience


def find_public_guide_privacy_issues(
    draft: StayGuideDraft | TravelGuideDraft,
) -> list[PublicationPolicyIssue]:
    issues: list[PublicationPolicyIssue] = []
    for path, raw_text in _visible_guide_text(draft):
        text = _normalize_public_text(raw_text)
        for matches, reason in _PRIVACY_TEXT_POLICIES:
            if matches(text):
                issues.append(PublicationPolicyIssue(path=path, reason=reason))
                break
    return issues


def _stay_guide_starter() -> dict[str, Any]:
    def section(summary: str) -> dict[str, Any]:
        return {"summary": summary, "visible": True}

    return {
        "title": "安心入住指南",
        "intro": "以下为所有住客通用的入住与离店建议，具体安排请以工作人员通知为准。",
        "sections": {
            "arrivalDeparture": section("出发前请确认天气与公共交通安排，并预留充足时间。"),
            "parking": section("停车安排可能因现场情况调整，请抵达前咨询工作人员。"),
            "wifiAndDevices": section("设施使用请遵循现场标识与工作人员指引。"),
            "houseRules": section("请爱护设施、保持安静，并遵守公共区域规定。"),
            "checkOut": section("离店前请带齐个人物品，并关闭无需使用的电器。"),
            "support": section("需要帮助时，请通过小程序内的公开联系方式联系工作人员。"),
            "faq": {
                "summary": "常见问题",
                "visible": True,
                "items": [
                    {
                        "question": "如何确认入住安排？",
                        "answer": "请以工作人员提供的通用通知为准。",
                    }
                ],
            },
        },
    }


def _travel_starter() -> dict[str, Any]:
    return {
        "title": "灵山湾旅游攻略",
        "intro": "以下是公开通用的出行灵感，实际开放情况请在出发前再次确认。",
        "recommendations": [
            {
                "id": "seaside-walk",
                "name": "海边漫步",
                "category": "must_see",
                "reason": "适合放慢节奏，欣赏公共海岸风景。",
                "approximateLocation": "灵山湾公共滨海区域",
                "suggestedDuration": "约一至两小时",
                "audiences": ["亲子", "朋友"],
                "visible": True,
            }
        ],
    }


def _draft_model(channel: str) -> type[StayGuideDraft] | type[TravelGuideDraft]:
    model = _DRAFT_MODELS.get(channel)
    if model is None:
        raise ContentValidationError("structured draft channel is unsupported")
    return model


def _validate_payload(channel: str, payload: Any) -> GuideDraft:
    model = _draft_model(channel)
    candidate = (
        payload.model_dump(by_alias=True, exclude_none=True)
        if isinstance(payload, BaseModel)
        else payload
    )
    try:
        return model.model_validate(candidate)
    except (TypeError, ValueError) as exc:
        raise ContentValidationError(
            "structured draft payload does not match the channel schema"
        ) from exc


def _starter_draft(channel: str) -> GuideDraft:
    payload = _stay_guide_starter() if channel == "stay_guide" else _travel_starter()
    return _validate_payload(channel, payload)


def _media_ids(draft: GuideDraft) -> set[str]:
    if isinstance(draft, StayGuideDraft):
        sections = draft.sections
        images = (
            sections.arrivalDeparture.image,
            sections.parking.image,
            sections.wifiAndDevices.image,
            sections.houseRules.image,
            sections.checkOut.image,
            sections.support.image,
            sections.faq.image,
        )
    else:
        images = tuple(item.image for item in draft.recommendations)
    return {image.mediaId for image in images if image is not None}


async def _validate_media_references(
    db: AsyncSession,
    draft: GuideDraft,
) -> None:
    media_ids = _media_ids(draft)
    if not media_ids:
        return
    rows = (
        await db.scalars(
            select(MiniappMedia).where(MiniappMedia.media_id.in_(media_ids))
        )
    ).all()
    available = {
        row.media_id
        for row in rows
        if row.media_type == "image"
        and row.reference_status in _DRAFT_USABLE_MEDIA_STATES
    }
    if available != media_ids:
        raise ContentValidationError(
            "structured draft references unavailable image media"
        )


def _result(
    *,
    channel: GuideChannel,
    revision: int,
    draft: GuideDraft,
    draft_dirty: bool,
    saved_at: datetime | None,
    saved_by: str | None,
) -> StructuredDraftRevision:
    return StructuredDraftRevision(
        channel=channel,
        revision=revision,
        draft=draft,
        draftDirty=draft_dirty,
        savedAt=saved_at,
        savedBy=saved_by,
    )


async def load_structured_draft(
    db: AsyncSession,
    *,
    channel: str,
    lock_workspace: bool = False,
) -> StructuredDraftRevision:
    model = _draft_model(channel)
    statement = (
        select(MiniappContentWorkspace, MiniappStructuredDraft)
        .outerjoin(
            MiniappStructuredDraft,
            MiniappStructuredDraft.channel == MiniappContentWorkspace.channel,
        )
        .where(MiniappContentWorkspace.channel == channel)
    )
    if lock_workspace:
        statement = statement.with_for_update(of=MiniappContentWorkspace)
    snapshot = (await db.execute(statement)).one_or_none()
    if snapshot is None:
        return _result(
            channel=channel,
            revision=0,
            draft=_starter_draft(channel),
            draft_dirty=False,
            saved_at=None,
            saved_by=None,
        )
    workspace, row = snapshot
    if row is None:
        return _result(
            channel=channel,
            revision=workspace.revision,
            draft=_starter_draft(channel),
            draft_dirty=workspace.is_dirty,
            saved_at=workspace.updated_at,
            saved_by=None,
        )
    try:
        draft = model.model_validate(row.payload)
    except (TypeError, ValueError) as exc:
        raise ContentValidationError("stored structured draft is invalid") from exc
    return _result(
        channel=channel,
        revision=workspace.revision,
        draft=draft,
        draft_dirty=workspace.is_dirty,
        saved_at=row.updated_at,
        saved_by=row.updated_by,
    )


async def save_structured_draft(
    db: AsyncSession,
    *,
    channel: str,
    expected_revision: int,
    payload: Any,
    actor_id: str,
) -> StructuredDraftRevision:
    draft = _validate_payload(channel, payload)
    serialized = draft.model_dump(by_alias=True, exclude_none=True)
    next_revision: int
    row: MiniappStructuredDraft

    try:
        async with db.begin_nested():
            workspace = await db.scalar(
                select(MiniappContentWorkspace)
                .where(MiniappContentWorkspace.channel == channel)
                .with_for_update()
            )
            if workspace is None:
                if expected_revision != 0:
                    raise DraftConflictError(
                        "saved draft revision no longer matches the request"
                    )
                next_revision = 1
                workspace = MiniappContentWorkspace(
                    channel=channel, revision=next_revision, is_dirty=True
                )
                db.add(workspace)
            else:
                if workspace.revision != expected_revision:
                    raise DraftConflictError(
                        "saved draft revision no longer matches the request"
                    )
                next_revision = expected_revision + 1
                workspace.revision = next_revision
                workspace.is_dirty = True

            await _validate_media_references(db, draft)
            row = await db.scalar(
                select(MiniappStructuredDraft)
                .where(MiniappStructuredDraft.channel == channel)
                .with_for_update()
            )
            if row is None:
                row = MiniappStructuredDraft(
                    channel=channel,
                    payload=serialized,
                    updated_by=actor_id,
                )
                db.add(row)
            else:
                row.payload = serialized
                row.updated_by = actor_id

            await log_action_tx(
                db,
                actor_id,
                f"content.{channel}.save",
                "miniapp_content_workspace",
                channel,
                after_data={"revision": next_revision},
            )
            await db.flush()
            await db.refresh(row, attribute_names=["updated_at"])
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DraftConflictError(
            "saved draft revision no longer matches the request"
        ) from exc
    except Exception:
        await db.rollback()
        raise

    return _result(
        channel=channel,
        revision=next_revision,
        draft=draft,
        draft_dirty=True,
        saved_at=row.updated_at,
        saved_by=actor_id,
    )


__all__ = [
    "find_public_guide_privacy_issues",
    "load_structured_draft",
    "save_structured_draft",
]
