"""操作者判定 —— 确定性代码算「人 / 系统 / 飞书保洁 / 未知」，不交 LLM 猜。

追溯「是不是我调的」答错的最大风险是把三类操作者搞混（尤其坑①飞书保洁 operator_id=None
却是真人）。规则钉死在这里，LLM 只复述带好标签的事实，不许自己推断人还是系统。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    kind: str            # "human" | "system-auto" | "cleaner-via-feishu" | "unknown"
    label: str           # 人话标签：显示名 / "系统自动" / "保洁（飞书完工）" / "未知"
    operator_id: str | None
    is_human: bool       # 判「是不是人调的」用；飞书保洁=True（真人）、系统=False


def classify_actor(
    operator_id: str | None,
    notes: str | None,
    display_name: str | None,
) -> Actor:
    """按 audit 行的 operator_id + notes 判定操作者类型。

    - operator_id == "system"        → 系统定时任务（workers），非人。
    - operator_id 为真实 user         → 人，label = display_name（缺则回退 id）。
    - operator_id is None + 飞书痕迹   → 坑①：飞书一键完工的真人保洁，非系统。
    - operator_id is None + 无痕迹     → 未知。
    """
    if operator_id == "system":
        return Actor(kind="system-auto", label="系统自动", operator_id="system", is_human=False)

    if operator_id:
        return Actor(
            kind="human",
            label=display_name or operator_id,
            operator_id=operator_id,
            is_human=True,
        )

    # operator_id is None —— 区分飞书保洁（真人）与真正未知
    note_text = notes or ""
    if "飞书" in note_text or "open_id=" in note_text:
        return Actor(
            kind="cleaner-via-feishu",
            label="保洁（飞书完工）",
            operator_id=None,
            is_human=True,
        )

    return Actor(kind="unknown", label="未知", operator_id=None, is_human=False)
