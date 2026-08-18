"""试运营房型角标。

某些新房型还在试运营、没进正式房型体系，只能靠 OTA/bypms 同步进来的
备注文本识别（房型名嵌在备注里）。命中关键词就在房态条上打一个角标，
让前台一眼看出这是试运营房型的单。

试运营结束把对应关键词从 ``TRIAL_KEYWORDS`` 删掉即可，无需改前端。
"""
from __future__ import annotations

# 关键词 → 房态条上显示的角标文字（子串匹配，命中即打标）。
# 注意 key 是「命中关键词」、value 是「显示文字」，两者可不同：
# 「甄选海景」用较长的串避免误命中美团「甄选」营销词，角标只显示「甄选」。
TRIAL_KEYWORDS: dict[str, str] = {
    "极海": "极海",
    "甄选海景": "甄选",
}


def trial_tag_for_notes(notes: str | None) -> str | None:
    """备注命中试运营关键词时返回角标文字，否则返回 None。"""
    if not notes:
        return None
    for keyword, tag in TRIAL_KEYWORDS.items():
        if keyword in notes:
            return tag
    return None
