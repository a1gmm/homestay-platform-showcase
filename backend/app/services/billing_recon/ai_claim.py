"""AI 干两件判断活：① 认领对不上的账单行（找系统单）② 当月账单诊断。
与 ai_mapping.py（列映射）分离。AI 只出建议——候选走白名单校验防幻觉，
诊断只叙述不造数；写库/改钱全在确定性代码里，AI 碰不到。月频调用，成本忽略。"""
from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import uuid4

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.services.billing_recon.ai_mapping import AiMappingError

logger = logging.getLogger(__name__)

CLAIM_CONFIDENCE_SINGLE = 0.85  # ≥ 前端一键「就是它」；< 摊开候选清单让用户挑
_MAX_CANDIDATES = 3

_MODEL = "deepseek-chat"
_BASE_URL = "https://api.deepseek.com"
_TIMEOUT = 12.0

_CLAIM_PROMPT = """你在做民宿 OTA 账单对账。有些账单行靠单号没对上系统订单，请你帮忙认领。
给你两组数据（JSON）：unmatched=对不上的账单行；pool=系统里的候选订单。
对每条 unmatched，从 pool 里找出最可能是同一笔预订的订单（比对客人姓名、入住/离店日期、金额、房型）。
以 JSON 对象返回，不要多余文字：
{"claims": {"<账单行 token>": [{"order_id": "<必须是候选 token>", "confidence": <0~1>, "reason_codes": ["same_guest"|"same_dates"|"similar_amount"|"same_room_type"|"same_platform_id_fragment"]}]}}
规则：只能使用输入中的匿名 token 和已给出的事实 code；找不到就返回空数组。不要输出姓名、金额、日期或解释文字。数据："""


class ClaimReasonCode(str, Enum):
    same_guest = "same_guest"
    same_dates = "same_dates"
    similar_amount = "similar_amount"
    same_room_type = "same_room_type"
    same_platform_id_fragment = "same_platform_id_fragment"


class ClaimCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    confidence: float = Field(ge=0, le=1)
    reason_codes: list[ClaimReasonCode] = Field(default_factory=list, max_length=5)


@dataclass(frozen=True)
class AnonymousClaimSample:
    payload: str
    row_tokens: dict[str, str]
    candidate_tokens: dict[str, str]


def _normalized(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _amount_bucket(left: object, right: object) -> str:
    a, b = _decimal(left), _decimal(right)
    if a is None or b is None:
        return "unknown"
    delta = abs(a - b)
    if delta <= Decimal("20"):
        return "le_20"
    ratio = delta / max(abs(b), Decimal("0.01"))
    if ratio <= Decimal("0.02"):
        return "le_2pct"
    if ratio <= Decimal("0.10"):
        return "le_10pct"
    return "gt_10pct"


def build_anonymous_claim_sample(unmatched: list[dict], pool: list[dict]) -> AnonymousClaimSample:
    """Build a request-local feature payload containing no raw identity or money values."""
    nonce = uuid4().hex[:10]
    row_tokens = {f"R-{nonce}-{idx}": str(row.get("no", "")) for idx, row in enumerate(unmatched, 1)}
    candidate_tokens = {
        f"C-{nonce}-{idx}": str(candidate.get("order_id", "")) for idx, candidate in enumerate(pool, 1)
    }
    rows = []
    for row_token, row in zip(row_tokens, unmatched):
        features = []
        for candidate_token, candidate in zip(candidate_tokens, pool):
            facts = []
            if _normalized(row.get("guest")) and _normalized(row.get("guest")) == _normalized(candidate.get("guest")):
                facts.append(ClaimReasonCode.same_guest.value)
            if row.get("checkin") and row.get("checkout") and (
                row.get("checkin"), row.get("checkout")
            ) == (candidate.get("checkin"), candidate.get("checkout")):
                facts.append(ClaimReasonCode.same_dates.value)
            amount_bucket = _amount_bucket(row.get("amount"), candidate.get("system_amount"))
            if amount_bucket in {"le_20", "le_2pct"}:
                facts.append(ClaimReasonCode.similar_amount.value)
            if _normalized(row.get("room_type")) and _normalized(row.get("room_type")) == _normalized(candidate.get("room_type")):
                facts.append(ClaimReasonCode.same_room_type.value)
            features.append({
                "candidate_token": candidate_token,
                "facts": facts,
                "amount_gap": amount_bucket,
            })
        rows.append({"row_token": row_token, "candidates": features})
    return AnonymousClaimSample(
        payload=json.dumps({"rows": rows}, ensure_ascii=True, separators=(",", ":")),
        row_tokens=row_tokens,
        candidate_tokens=candidate_tokens,
    )


def parse_claims_response(text: str, valid_ids: set[str]) -> dict[str, list[ClaimCandidate]]:
    """AI 认领结果 → {账单单号: [候选]}。白名单丢幻觉、降序、封顶 3。
    解析失败/畸形一律返回 {}（当作没认出来，不炸）。"""
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    claims = raw.get("claims") if isinstance(raw, dict) else None
    if not isinstance(claims, dict):
        return {}
    out: dict[str, list[ClaimCandidate]] = {}
    for no, items in claims.items():
        if not isinstance(items, list):
            continue
        cands: list[ClaimCandidate] = []
        for it in items:
            try:
                c = ClaimCandidate.model_validate(it)
            except ValidationError:
                continue
            if c.order_id in valid_ids:
                cands.append(c)
        if cands:
            cands.sort(key=lambda c: c.confidence, reverse=True)
            out[str(no)] = cands[:_MAX_CANDIDATES]
    return out


def parse_anonymous_claims_response(
    text: str,
    *,
    row_tokens: dict[str, str],
    candidate_tokens: dict[str, str],
) -> dict[str, list[ClaimCandidate]]:
    parsed = parse_claims_response(text, set(candidate_tokens))
    resolved: dict[str, list[ClaimCandidate]] = {}
    for row_token, candidates in parsed.items():
        bill_no = row_tokens.get(row_token)
        if not bill_no:
            continue
        mapped = [
            candidate.model_copy(update={"order_id": candidate_tokens[candidate.order_id]})
            for candidate in candidates
            if candidate.order_id in candidate_tokens
        ]
        if mapped:
            resolved[bill_no] = mapped
    return resolved


def build_claim_sample(unmatched: list[dict], pool: list[dict]) -> str:
    """Backward-compatible alias returning only the anonymous payload."""
    return build_anonymous_claim_sample(unmatched, pool).payload


async def propose_claims(unmatched: list[dict], pool: list[dict]) -> dict[str, list[ClaimCandidate]]:
    """调用 DeepSeek 做账单行认领。无 unmatched 或无 pool 返回空字典（不调网络）。
    缺 DEEPSEEK_API_KEY 抛 AiMappingError。"""
    if not unmatched or not pool:
        return {}
    if not settings.DEEPSEEK_API_KEY:
        raise AiMappingError("DEEPSEEK_API_KEY 未配置，无法做账单认领")

    sample = build_anonymous_claim_sample(unmatched, pool)
    client = AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=_BASE_URL,
        timeout=_TIMEOUT,
        max_retries=0,
    )
    resp = await client.chat.completions.create(
        model=_MODEL, max_tokens=2000,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": _CLAIM_PROMPT + sample.payload}],
    )
    text = resp.choices[0].message.content or ""
    return parse_anonymous_claims_response(
        text,
        row_tokens=sample.row_tokens,
        candidate_tokens=sample.candidate_tokens,
    )


class BatchThemeCode(str, Enum):
    platform_short_pay = "platform_short_pay"
    normal_fee_variance = "normal_fee_variance"
    compensation_deduction = "compensation_deduction"
    unmatched_orders = "unmatched_orders"


class DiffReasonCode(str, Enum):
    commission_or_fee = "commission_or_fee"
    compensation_deduction = "compensation_deduction"
    platform_unsettled = "platform_unsettled"
    amount_mismatch_unknown = "amount_mismatch_unknown"


class BatchDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme_codes: list[BatchThemeCode] = Field(default_factory=list, max_length=4)
    per_row: dict[str, DiffReasonCode] = Field(default_factory=dict)


_DIAG_PROMPT = """根据匿名的 OTA 对账分类计数选择主题 code。只返回 JSON：
{"theme_codes": ["platform_short_pay"|"normal_fee_variance"|"compensation_deduction"|"unmatched_orders"], "per_row": {}}
不得输出解释文字、金额、姓名、订单号或新增字段。数据："""


def parse_diagnosis_response(text: str) -> BatchDiagnosis:
    """AI 诊断结果解析。畸形→空 BatchDiagnosis（不抛异常）。"""
    try:
        return BatchDiagnosis.model_validate_json(text)
    except (ValidationError, ValueError):
        return BatchDiagnosis()


async def diagnose(aggregates: dict, fix_rows: list[dict]) -> BatchDiagnosis:
    """调用 DeepSeek 做当月账单诊断。无 aggregates 和 fix_rows 返回空 BatchDiagnosis()（不调网络）。
    缺 DEEPSEEK_API_KEY 抛 AiMappingError。"""
    if not aggregates and not fix_rows:
        return BatchDiagnosis()
    if not settings.DEEPSEEK_API_KEY:
        raise AiMappingError("DEEPSEEK_API_KEY 未配置，无法做账单诊断")
    client = AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=_BASE_URL,
        timeout=_TIMEOUT,
        max_retries=0,
    )
    allowed_counts = {
        key: int(aggregates.get(key, 0) or 0)
        for key in ("fix_amount", "appeal", "broken_link", "compensation", "manual_review")
    }
    payload = json.dumps({"counts": allowed_counts}, ensure_ascii=True, separators=(",", ":"))
    resp = await client.chat.completions.create(
        model=_MODEL, max_tokens=1500,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": _DIAG_PROMPT + payload}],
    )
    return parse_diagnosis_response(resp.choices[0].message.content or "")
