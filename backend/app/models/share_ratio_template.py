"""
v2-issue#1 — 业主分成比例模板枚举。

王总参考 PMS 系统使用模板下拉（"四六分"等）而非数字直填。本模块定义模板枚举
+ 业主百分比映射，前后端共用。

兼容策略：Room.share_ratio_normal/_trial/_owner_self 仍是 Decimal(4,3) 不变。
新增 share_ratio_template_normal/_trial/_owner_self 三个 enum 字段（v2-issue#2 加），
模板字段是"显示意图"，Decimal 字段是"实际计算值"。两者保持同步：
  - UI 选模板 → 自动写 Decimal
  - 通过老接口直接写 Decimal → 反查最近的模板（容错为 custom）
"""
from __future__ import annotations

import enum
from decimal import Decimal


class ShareRatioTemplate(str, enum.Enum):
    """业主分成比例模板。命名按"业主分成百分比"避免歧义。"""
    owner_10 = "owner_10"     # 一九分（业主 10%）
    owner_20 = "owner_20"     # 二八分（业主 20%）
    owner_30 = "owner_30"     # 三七分（业主 30%）
    owner_40 = "owner_40"     # 四六分（业主 40%）
    owner_50 = "owner_50"     # 五五分（业主 50%）
    owner_60 = "owner_60"     # 六四分（业主 60%）
    owner_70 = "owner_70"     # 七三分（业主 70%）
    owner_80 = "owner_80"     # 八二分（业主 80%）
    owner_90 = "owner_90"     # 九一分（业主 90%）
    owner_full = "owner_full" # 业主全担（业主 100%）
    none = "none"             # 业主不分成（业主 0%）
    custom = "custom"         # 自定义数字（兼容 v1 任意 Decimal 值）


# 模板 → 业主分成百分比（Decimal）
TEMPLATE_TO_RATIO: dict[ShareRatioTemplate, Decimal] = {
    ShareRatioTemplate.owner_10: Decimal("0.100"),
    ShareRatioTemplate.owner_20: Decimal("0.200"),
    ShareRatioTemplate.owner_30: Decimal("0.300"),
    ShareRatioTemplate.owner_40: Decimal("0.400"),
    ShareRatioTemplate.owner_50: Decimal("0.500"),
    ShareRatioTemplate.owner_60: Decimal("0.600"),
    ShareRatioTemplate.owner_70: Decimal("0.700"),
    ShareRatioTemplate.owner_80: Decimal("0.800"),
    ShareRatioTemplate.owner_90: Decimal("0.900"),
    ShareRatioTemplate.owner_full: Decimal("1.000"),
    ShareRatioTemplate.none: Decimal("0.000"),
    # custom 不在此 map（fallback 走 Decimal 字段）
}


# 中文 label（前端共用）
TEMPLATE_LABELS: dict[ShareRatioTemplate, str] = {
    ShareRatioTemplate.owner_10: "一九分（业主 10%）",
    ShareRatioTemplate.owner_20: "二八分（业主 20%）",
    ShareRatioTemplate.owner_30: "三七分（业主 30%）",
    ShareRatioTemplate.owner_40: "四六分（业主 40%）",
    ShareRatioTemplate.owner_50: "五五分（业主 50%）",
    ShareRatioTemplate.owner_60: "六四分（业主 60%）",
    ShareRatioTemplate.owner_70: "七三分（业主 70%）",
    ShareRatioTemplate.owner_80: "八二分（业主 80%）",
    ShareRatioTemplate.owner_90: "九一分（业主 90%）",
    ShareRatioTemplate.owner_full: "业主全担（100%）",
    ShareRatioTemplate.none: "业主不分成（0%）",
    ShareRatioTemplate.custom: "自定义",
}


def template_for_ratio(value: Decimal | float | str | None) -> ShareRatioTemplate:
    """根据 Decimal 数字反查最接近的模板；找不到精确匹配时返回 custom。

    用例：旧数据 owner_share_ratio=0.6 → owner_60；任意非整数比如 0.55 → custom。
    """
    if value is None:
        return ShareRatioTemplate.custom
    try:
        d = Decimal(str(value)).quantize(Decimal("0.001"))
    except (ValueError, ArithmeticError):
        return ShareRatioTemplate.custom
    for tpl, ratio in TEMPLATE_TO_RATIO.items():
        if d == ratio:
            return tpl
    return ShareRatioTemplate.custom


def ratio_for_template(template: ShareRatioTemplate, custom_value: Decimal | None = None) -> Decimal:
    """根据模板返回业主分成数字。custom 模板时取 custom_value，否则查表。"""
    if template == ShareRatioTemplate.custom:
        return custom_value if custom_value is not None else Decimal("0")
    return TEMPLATE_TO_RATIO[template]
