"""DeepSeek 只识别陌生表头坐标；不接收姓名，不读取或计算金额。"""

import json
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.config import settings


ALLOWED_COLUMNS = {"date", "floor", "room", "customer", "category", "receipt_amount", "expense_amount", "summary"}


class UtilityMappingError(RuntimeError):
    pass


class UtilityColumnMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["receipt", "expense"]
    sheet: str
    header_row: int = Field(ge=0, le=19)
    columns: dict[str, int]

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, value: dict[str, int]) -> dict[str, int]:
        if not value or not set(value) <= ALLOWED_COLUMNS:
            raise ValueError("unknown column key")
        if any(not isinstance(index, int) or index < 0 or index >= 100 for index in value.values()):
            raise ValueError("column index out of range")
        return value


def parse_mapping_response(text: str) -> UtilityColumnMapping:
    try:
        return UtilityColumnMapping.model_validate_json(text)
    except ValidationError as exc:
        raise UtilityMappingError("AI 认列结果不合规范") from exc


def _shape(value: object) -> str:
    if value is None or str(value).strip() == "":
        return "EMPTY"
    if isinstance(value, (int, float)):
        return "NUMBER"
    text = str(value).strip()
    # 只保留可能是表头/科目的短标签；其他文本（姓名、房号、摘要）全部匿名。
    safe_tokens = ("日期", "楼层", "房", "金额", "收款", "付款", "费用", "科目", "摘要", "客户", "水费", "电费")
    return text if len(text) <= 20 and any(token in text for token in safe_tokens) else "TEXT"


async def ai_column_mapping(filename: str, sheets: dict[str, list[tuple]]) -> UtilityColumnMapping:
    if not settings.DEEPSEEK_API_KEY:
        raise UtilityMappingError("AI 未配置，陌生表头需要人工确认")
    tokens = {f"S{index}": name for index, name in enumerate(sheets, 1)}
    sample = {
        token: [[_shape(cell) for cell in row[:100]] for row in sheets[name][:8]]
        for token, name in tokens.items()
    }
    prompt = (
        "识别一份民宿水电流水 Excel 的文件角色和列坐标。只返回 JSON："
        "role(receipt或expense), sheet(S1等), header_row(0起), columns。"
        "columns键只能是date/floor/room/customer/category/receipt_amount/expense_amount/summary。"
        "receipt必须有date和receipt_amount；expense必须有date和expense_amount。不要计算金额。样本："
        + json.dumps(sample, ensure_ascii=False, separators=(",", ":"))
    )
    client = AsyncOpenAI(api_key=settings.DEEPSEEK_API_KEY, base_url="https://api.deepseek.com", timeout=12, max_retries=0)
    response = await client.chat.completions.create(
        model="deepseek-chat", max_tokens=600, response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    content = response.choices[0].message.content
    if not content:
        raise UtilityMappingError("AI 未返回认列结果")
    mapping = parse_mapping_response(content)
    if mapping.sheet not in tokens:
        raise UtilityMappingError("AI 返回了未知工作表")
    return mapping.model_copy(update={"sheet": tokens[mapping.sheet]})
