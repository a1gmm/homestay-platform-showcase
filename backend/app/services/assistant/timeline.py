"""确定性时间线渲染 —— 追溯的承重事实（谁/几点/改了啥）全由代码算。

LLM 只负责末尾一句人话总结（narrate.py）；这里产出的 text/rows 是「底稿」，
前端并排展示、老板一眼可核。承重事实不许幻觉。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.datetime_helpers import to_cn
from app.models.order import ORDER_STATUS_LABELS
from app.services.assistant.actors import Actor, classify_actor

# 动作码 → 人话动词。未收录的动作回退成原始码（诚实，不瞎编）。
_VERB: dict[str, str] = {
    "order.create": "建单",
    "order.update": "改单",
    "order.update_after_completed": "完成后改单",
    "order.update_daily_price": "改单日房价",
    "order.assign_room": "排房",
    "order.transfer_room": "换房",
    "order.swap_room": "对调房间",
    "order.transition": "改状态",
    "order.cancel": "取消订单",
    "order.delete": "删除订单",
    "order.link_continuation": "关联续住",
    "order.unlink_continuation": "解除续住关联",
    "order.lock_extend": "延长门锁",
    "order.lock_resend_card": "重发门锁码",
    "order.lock_code_view": "查看门锁码",
    "order.auto_completed_checkout": "自动完成退房",
    "auto_cancel_stale_order": "自动取消弃单",
    "auto_transitioned": "自动流转状态",
    "auto_clear_stale_pending_clean": "自动清理待清洁",
    "task.cleaning_done_feishu": "保洁完工",
    "task.cleaning_start_feishu": "保洁开始",
}

# 完整快照会有的锚点键（order_snapshot 一定含 order_id）；缺它 = 精简 diff（坑②）。
_FULL_SNAPSHOT_MARKER = "order_id"

# 完整快照里关注的可比对字段 → 中文标签。
_TRACKED_FIELDS: list[tuple[str, str]] = [
    ("check_in_date", "入住"),
    ("check_out_date", "离店"),
    ("room_id", "房间"),
    ("actual_price", "房费"),
    ("order_status", "状态"),
    ("deposit_status", "押金状态"),
    ("deposit", "押金"),
]

_PARTIAL_HEDGE = "（改前值不全）"


@dataclass
class TimelineEvent:
    at: datetime                       # 原始 UTC/aware 时间
    actor: Actor
    action: str                        # 原始动作码
    verb: str                          # 人话动词
    changes: list[str] = field(default_factory=list)
    partial_snapshot: bool = False
    note: str | None = None


def _fmt_date(v) -> str:
    """'2026-07-31' → '7/31'；非日期字符串原样返回。"""
    if not v:
        return "—"
    s = str(v)
    parts = s.split("-")
    if len(parts) == 3 and parts[0].isdigit():
        try:
            return f"{int(parts[1])}/{int(parts[2])}"
        except ValueError:
            return s
    return s


def _fmt_value(field_name: str, v) -> str:
    if v is None:
        return "—"
    if field_name in ("check_in_date", "check_out_date"):
        return _fmt_date(v)
    if field_name == "order_status":
        return ORDER_STATUS_LABELS.get(str(v), str(v))
    if field_name in ("actual_price", "deposit"):
        return f"¥{v}"
    return str(v)


def _diff_full(before: dict, after: dict) -> list[str]:
    changes: list[str] = []
    for key, label in _TRACKED_FIELDS:
        b = before.get(key)
        a = after.get(key)
        if b == a:
            continue
        # 一方缺该键（快照未含）不算变化，避免误报
        if key not in before or key not in after:
            continue
        changes.append(f"{label} {_fmt_value(key, b)}→{_fmt_value(key, a)}")
    return changes


def _diff_slim(before: dict | None, after: dict | None) -> list[str]:
    """精简 diff 行的旧值回推（换房/排房/改价）。best-effort，旧值可能不全。"""
    before = before or {}
    after = after or {}
    pairs = [
        ("old_room_id", "new_room_id", "房间"),
        ("old_room", "new_room", "房间"),
        ("old_price", "new_price", "房费"),
        ("old_order_actual", "new_order_total", "整单房费"),
    ]
    changes: list[str] = []
    for old_k, new_k, label in pairs:
        if old_k in before or new_k in after or old_k in after or new_k in before:
            old_v = before.get(old_k, after.get(old_k))
            new_v = after.get(new_k, before.get(new_k))
            changes.append(f"{label} {old_v if old_v is not None else '？'}→{new_v if new_v is not None else '？'}")
    return changes


def build_events(rows, name_map: dict[str, str]) -> list[TimelineEvent]:
    """把 audit 行（按时间升序）富化成 TimelineEvent（actor + 变化 + partial 标记）。

    rows: 具 .action/.operator_id/.before_data/.after_data/.notes/.created_at 的对象。
    name_map: operator_id → display_name。
    """
    events: list[TimelineEvent] = []
    for r in rows:
        before = r.before_data or None
        after = r.after_data or None
        actor = classify_actor(
            operator_id=r.operator_id,
            notes=r.notes,
            display_name=name_map.get(r.operator_id) if r.operator_id else None,
        )
        # 完整快照：before 含 order_id（create 的 before=None 不算精简，是本来就没有前值）
        partial = before is not None and _FULL_SNAPSHOT_MARKER not in before

        if partial:
            changes = _diff_slim(before, after)
        elif before and after:
            changes = _diff_full(before, after)
        else:
            changes = []

        events.append(TimelineEvent(
            at=r.created_at,
            actor=actor,
            action=r.action,
            verb=_VERB.get(r.action, r.action),
            changes=changes,
            partial_snapshot=partial,
            note=r.notes,
        ))
    return events


def render_timeline(events: list[TimelineEvent]) -> dict:
    """确定性渲染：返回 {text, rows}。text 供 LLM 引用+前端兜底，rows 供前端结构化底稿。"""
    lines: list[str] = []
    rows_out: list[dict] = []
    for ev in events:
        cn = to_cn(ev.at)
        stamp = f"{cn.month}月{cn.day}日 {cn.strftime('%H:%M')}"
        change_text = "：" + "，".join(ev.changes) if ev.changes else ""
        hedge = _PARTIAL_HEDGE if ev.partial_snapshot else ""
        line = f"{stamp} {ev.actor.label} {ev.verb}{change_text}{hedge}"
        lines.append(line)
        rows_out.append({
            "time": stamp,
            "actor_label": ev.actor.label,
            "actor_kind": ev.actor.kind,
            "is_human": ev.actor.is_human,
            "verb": ev.verb,
            "action": ev.action,
            "changes": ev.changes,
            "partial_snapshot": ev.partial_snapshot,
            "hedge": _PARTIAL_HEDGE if ev.partial_snapshot else "",
            "note": ev.note,
        })
    return {"text": "\n".join(lines), "rows": rows_out}
