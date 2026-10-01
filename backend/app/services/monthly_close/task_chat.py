"""Chat entry and durable, reviewable results of background month tasks.

An overall goal permits investigation and proposal preparation, never financial
execution. Prepared results use the same actor-bound confirmation path as chat.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

from fastapi import HTTPException
from sqlalchemy import select

from app.models.monthly_close_control import MonthlyCloseMessage, MonthlyCloseRun
from app.services.financial_case.chat import previous_reply
from app.services.monthly_close.assistant import (
    AssistantReply, _complete_run, _ensure_conversation, _stable_id,
)
from app.services.monthly_close.cleaning_work_chat import digest

VERSION = "monthly-close-task/v1"


def has_unsupported_task_scope(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if re.search(r"仅|只|先不|暂不|(?:先|暂)(?:做|对|处理|核对|核实)|除了|除外|不含|不要|不用|不做|不处理|不核对|别|取消|(?:公司|业主)(?:承担|支付|付款)|删除|忽略|自动确认|直接入账|跳过|绕过", compact):
        return True
    category = re.search(r"保洁|打扫|布草|洗涤|水电|物业|维保|银行|工资|OTA|平台账单", compact, re.I)
    whole = re.search(r"整月|完整月结|全部资料|所有资料|本月对账|本月结算|本月月结", compact)
    return bool(category and not whole)


def is_whole_month_request(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    # Questions, negations, hypothetical discussion and commands for another
    # individual operation must keep their existing conversational routing.
    if has_unsupported_task_scope(compact) or re.search(r"[?？]|如果|假如|能否|能不能|可以吗|为什么|怎么|如何", compact):
        return False
    return bool(re.search(r"月结|对账|结算", compact)
                and re.search(r"全部|整月|完整|所有|本月|这个月", compact)
                and re.search(r"帮我|开始|完成|做完|处理完|继续处理|一并处理", compact))


def validate_task_month(text: str, billing_month: str) -> None:
    """The selected cycle cannot silently replace an explicitly named period."""
    compact = re.sub(r"\s+", "", text)
    months = re.findall(r"(?:(20\d{2})[-年])?(0?[1-9]|1[0-2])月", compact)
    declared = {f"{year or billing_month[:4]}-{int(month):02d}" for year, month in months}
    declared.update(re.findall(r"20\d{2}-\d{2}(?!\d)", compact))
    month_token = r"(?:0?[1-9]|1[0-2]|[一二三四五六七八九十]{1,3})"
    multiple_months = re.search(rf"跨月|全年|年度|季度|(?<!\d){month_token}[、至到~～—-]{month_token}月", compact)
    if (declared and declared != {billing_month}) or multiple_months:
        raise HTTPException(422, "整月任务每次处理当前选中的一个月份。请切换到要处理的月份后开始。")


async def persist_result(db, cycle, actor, *, key: str, tool: str | None,
                         facts: dict, message: str, intent: str = "action_plan",
                         manifest: str = VERSION, prepare=None) -> AssistantReply:
    """One atomic result/proposal per exact task stage and evidence fingerprint.

    The tool event is deliberately not attributed to a user instruction. Builders
    passed through prepare must only stage database changes, never commit or
    execute financial commands. The terminal message and proposal commit together.
    """
    run_id = _stable_id("MCR-", key)
    existing = await previous_reply(db, cycle, actor, run_id)
    if existing is not None:
        return existing
    conversation_id = _stable_id("MCCV-", f"{cycle.cycle_id}:finance")
    await _ensure_conversation(db, conversation_id=conversation_id,
                               cycle_id=cycle.cycle_id, scope="finance", actor_id=actor["user_id"])
    run = await db.get(MonthlyCloseRun, run_id)
    if run is not None:
        # An incomplete previous transaction must roll back as a unit; never
        # replace a terminal run or an actor/cycle mismatch with new authority.
        raise HTTPException(409, "任务方案状态需要重新核验，请刷新任务")
    run = MonthlyCloseRun(run_id=run_id, cycle_id=cycle.cycle_id,
        conversation_id=conversation_id, trigger_type="schedule", actor_type="system",
        actor_id=actor["user_id"], permission_snapshot={"role": "admin", "task_preparation": True},
        status="running", prompt_version=VERSION, tool_manifest_version=manifest,
        input_hash=digest(key), started_at=datetime.now(timezone.utc))
    db.add(run)
    await db.flush()
    db.add(MonthlyCloseMessage(message_id=_stable_id("MCM-", f"task-event:{key}"),
        cycle_id=cycle.cycle_id, conversation_id=conversation_id, run_id=run_id,
        role="tool_event", content_redacted="月结任务依据当前原件与账本准备核对结果。",
        attachments=[], visibility_scope="finance", created_by=actor["user_id"]))
    if prepare is not None:
        facts = await prepare(run_id)
    reply = AssistantReply(message=message, intent=intent, tool=tool, facts=facts,
        recommended_action={}, narration_degraded=False,
        conversation_id=conversation_id, run_id=run_id)
    result, _ = await _complete_run(db, run_id, reply=reply, status="succeeded",
                                    error_code=None, error_detail_redacted=None)
    return result


async def answer_task_command(db, cycle, current, text, attachment_ids=None, context_run_id=None):
    from app.services.monthly_close.task_runtime import read_task, resume_task, start_task
    compact = text.strip()
    if compact == "查看任务方案":
        if current.get("role") != "admin" or not context_run_id:
            raise HTTPException(403, "仅管理员可查看自己的任务方案")
        reply = await previous_reply(db, cycle, current, context_run_id)
        run = await db.get(MonthlyCloseRun, context_run_id)
        if reply is None or run is None or not run.permission_snapshot.get("task_preparation"):
            raise HTTPException(404, "没有找到当前月份的任务方案")
        return reply
    if not is_whole_month_request(compact) or attachment_ids:
        return None
    if current.get("role") != "admin":
        raise HTTPException(403, "月结任务仅限管理员")
    validate_task_month(compact, cycle.billing_month)
    task = await read_task(db, cycle.cycle_id, current["user_id"])
    if task is None:
        task = await start_task(db, cycle.cycle_id, current["user_id"], compact)
    elif task["status"] in {"paused", "failed", "waiting_user", "waiting_approval"}:
        task = await resume_task(db, cycle.cycle_id, current["user_id"], expected_revision=task["revision"])
    message = (
        "本月任务已完成交付核验，可在任务中下载业主结算单和订单明细。正式业主确认、付款和关账仍以系统中的实际记录为准。"
        if task["status"] == "succeeded" else
        "已开始本月完整核对。我会读取现有资料、准备补齐与费用方案，确认后继续推进，最后核验业主结算单和订单明细。进度保存在本月任务中，关闭页面后仍会继续；缺少的事实会集中列出。"
    )
    return await persist_result(db, cycle, current,
        key=f"task-start:{task['task_id']}:{task['revision']}", tool=None, facts={},
        message=message,
        intent="clarification")
