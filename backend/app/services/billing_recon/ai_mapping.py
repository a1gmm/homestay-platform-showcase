# backend/app/services/billing_recon/ai_mapping.py
"""AI 只干一件事：看表头样本，认出哪列是什么（BillMapping）。

行抽取/校验/对账全在 parser.py / engine.py（确定性代码）。AI 认错列的后果
被校验闸兜住（总额对不上整批拒收），不会静默错数。
月频调用（每月一张账单），成本忽略不计；模型用 DeepSeek deepseek-chat（json_object 模式）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.services.billing_recon.parser import BillMapping

_MODEL = "deepseek-chat"
_BASE_URL = "https://api.deepseek.com"
# 月频调用、单次请求，宁可等也别把用户卡在 SDK 默认超时之外；上传端点会把本异常转 503。
_TIMEOUT = 12.0

_SAFE_LABELS = (
    "订单", "客人", "姓名", "入住", "离店", "结算", "金额", "付款", "房型",
    "类型", "退款", "赔款", "罚款", "正常", "佣金", "服务费", "合计", "汇总",
)
_ORDER_ID_RE = re.compile(r"^\d{8,}$")
_DATE_RE = re.compile(r"^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}")


@dataclass(frozen=True)
class AnonymousColumnSample:
    payload: str
    sheet_tokens: dict[str, str]


class AiMappingError(RuntimeError):
    """AI 认列这一步自身失败（缺 key / 空响应 / 结果不合规范）→ 端点转 503。

    继承 RuntimeError 只为兼容既有调用方/测试；端点不再宽泛地把 RuntimeError 当 503，
    否则任何深处冒上来的 bug 都会被伪装成"AI 服务暂时不可用"。
    """

_PROMPT = """你在做民宿 OTA 账单对账的第一步：识别表格结构。以 JSON 对象返回如下字段，不要输出任何多余文字。

下面是一个账单工作簿每个 sheet 的前若干行采样（JSON，行列均为 0 起始下标）。

找出「订单明细」所在的 sheet，并以 JSON 对象返回：
- sheet: 明细 sheet token（如 S1）
- header_row: 表头行的行号（整数）
- col_order_no/col_guest/col_checkin/col_checkout/col_amount: 订单号/客人姓名/入住日期/离店日期/结算金额(酒店实际到手结算价) 的列号（整数）
- col_row_type: 行类型列（如"订单类型"）的列号，没有则 null（整数或 null）
- row_type_map: 行类型列的取值到 normal(正常)/refund(退款)/compensation(赔款/罚款) 的映射（对象），只映射数据行会出现的值
- platform_guess: 根据表头/酒店名/文案判断这份账单出自哪个平台，取值 "ctrip"|"meituan"|"other"

样本只含表头和单元格类型，不含真实客人、订单、日期或金额。不要猜金额。只依据样本作答。样本："""


class MappingCoordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet: str
    header_row: int = Field(ge=0)
    col_order_no: int = Field(ge=0)
    col_guest: int = Field(ge=0)
    col_checkin: int = Field(ge=0)
    col_checkout: int = Field(ge=0)
    col_amount: int = Field(ge=0)
    col_row_type: int | None = Field(default=None, ge=0)
    row_type_map: dict[str, str] = Field(default_factory=dict)
    platform_guess: str = "ctrip"


def _cell_shape(value: object) -> str:
    if value is None or str(value).strip() == "":
        return "EMPTY"
    if isinstance(value, (datetime, date)):
        return "DATE"
    if isinstance(value, (int, float, Decimal)):
        return "NUMBER"
    text = str(value).strip()
    if _ORDER_ID_RE.fullmatch(text):
        return "ORDER_ID"
    if _DATE_RE.match(text):
        return "DATE"
    if len(text) <= 30 and any(label in text for label in _SAFE_LABELS):
        return text
    return "TEXT"


def build_anonymous_sample(sheets: dict[str, list[list]]) -> AnonymousColumnSample:
    sample: dict[str, list[list[str]]] = {}
    sheet_tokens: dict[str, str] = {}
    for idx, (name, rows) in enumerate(sheets.items(), 1):
        token = f"S{idx}"
        sheet_tokens[token] = name
        sample[token] = [[_cell_shape(cell) for cell in row[:24]] for row in rows[:8]]
    return AnonymousColumnSample(
        payload=json.dumps(sample, ensure_ascii=False, separators=(",", ":")),
        sheet_tokens=sheet_tokens,
    )


def build_sample(sheets: dict[str, list[list]]) -> str:
    return build_anonymous_sample(sheets).payload


def parse_mapping_response(text: str) -> BillMapping:
    try:
        return BillMapping.model_validate_json(text)
    except ValidationError as e:
        raise AiMappingError("AI 认列结果不合规范，请重试") from e


def finalize_mapping(
    coordinates: MappingCoordinates,
    *,
    sheets: dict[str, list[list]],
    sheet_tokens: dict[str, str],
) -> BillMapping:
    sheet_name = sheet_tokens.get(coordinates.sheet)
    if not sheet_name or sheet_name not in sheets:
        raise AiMappingError("AI 认列结果不合规范，请重试")
    total = Decimal("0")
    rows = sheets[sheet_name]
    for row in rows[coordinates.header_row + 1:]:
        if coordinates.col_order_no >= len(row) or coordinates.col_amount >= len(row):
            continue
        order_no = str(row[coordinates.col_order_no] or "").strip().split(".")[0]
        if not order_no.isdigit() or len(order_no) < 8:
            continue
        raw_amount = str(row[coordinates.col_amount] or "").replace(",", "").replace("¥", "").strip()
        try:
            total += Decimal(raw_amount)
        except InvalidOperation:
            continue
    return BillMapping(
        **coordinates.model_dump(exclude={"sheet"}),
        sheet=sheet_name,
        summary_total=float(total.quantize(Decimal("0.01"))),
    )


async def ai_column_mapping(sheets: dict[str, list[list]]) -> BillMapping:
    if not settings.DEEPSEEK_API_KEY:
        raise AiMappingError("DEEPSEEK_API_KEY 未配置，无法做账单列映射")

    sample = build_anonymous_sample(sheets)
    client = AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=_BASE_URL,
        timeout=_TIMEOUT,
        max_retries=0,
    )
    resp = await client.chat.completions.create(
        model=_MODEL,
        max_tokens=1500,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": _PROMPT + sample.payload}],
    )
    text = resp.choices[0].message.content
    if not text:
        raise AiMappingError("AI 未返回内容，请重试")
    try:
        coordinates = MappingCoordinates.model_validate_json(text)
    except ValidationError as exc:
        raise AiMappingError("AI 认列结果不合规范，请重试") from exc
    return finalize_mapping(coordinates, sheets=sheets, sheet_tokens=sample.sheet_tokens)
