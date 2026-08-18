"""卡片动作编排——「打扫完了」等按钮点击的业务编排，HTTP 回调与 ws 长连接共用。

从 api/v1/feishu_callback 提出来的纯编排层：完工回写 + 审计 + 退押金提醒 +
toast 文案。入口层（HTTP endpoint / ws 处理器）只负责传输协议差异
（验签/challenge/老管道应答格式 vs SDK 事件对象）。
"""
import logging

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import service_fee_ledger
from app.services.audit import log_action
from app.services.cleaning import complete_cleaning_for_task, start_cleaning_for_task
from app.services.feishu_card_callback import build_toast_response

logger = logging.getLogger(__name__)


async def process_card_action(
    db: AsyncSession,
    *,
    value: dict,
    open_id: str,
    background_tasks: BackgroundTasks,
    message_id: str = "",
) -> dict:
    """处理一次卡片动作，返回 toast dict（调用方按传输协议转换应答格式）。

    clean_done → 完工回写（幂等）+ 审计 + 首次完工且有订单时退押金提醒
    （挂 background_tasks 应答后发）；deposit_done → 把点中的退押金卡改灰 + 审计；
    未知动作回「已收到」兜底。message_id 为点击事件自带的 open_message_id
    （deposit_done 改灰要用，点哪张灰哪张）。
    """
    if value.get("action") in ("inspect_done", "deposit_refunded"):
        return await _handle_inspect_deposit(db, value, open_id, message_id, background_tasks)

    if value.get("action") == "deposit_done":  # 老单按钮兼容（在飞的旧卡）
        return await _handle_deposit_done(db, value, open_id, message_id, background_tasks)

    if value.get("action") in ("cr_apply", "cr_done", "cr_approve"):
        return await _handle_cleaning_request_action(
            db, value, open_id, message_id, background_tasks
        )

    if value.get("action") == "clean_start":
        return await _handle_clean_start(db, value, open_id, message_id, background_tasks)

    if value.get("action") != "clean_done":
        logger.warning("飞书卡片动作未识别 action=%s", value.get("action"))
        return build_toast_response("已收到")

    try:
        result = await complete_cleaning_for_task(
            db,
            task_id=value.get("task_id"),
            room_id=value.get("room_id"),
            operator_open_id=open_id,
            background_tasks=background_tasks,  # 撤码应答后执行，守住飞书 3s 红线
        )
    except service_fee_ledger.CheckoutServiceFeeWrongMonthError as exc:
        logger.warning("保洁完工被服务费账本冲突阻断: %s", exc)
        return build_toast_response(
            "⚠️ 服务费账本月份冲突，未完成回写，请联系财务核对",
            toast_type="error",
        )
    # 完工审计已随 complete_cleaning_for_task 同事务写入（见 services/cleaning）。
    # task 和房间都没解析到 → 什么都没写，不能回假成功（2026-07-08 事故的
    # 病根就是点击看着成功、状态从没写进去）。
    if result.get("room_name") is None and not result.get("task_found"):
        logger.warning(
            "飞书完工回调未命中任何任务/房间 task_id=%s room_id=%s",
            value.get("task_id"), value.get("room_id"),
        )
        return build_toast_response("⚠️ 未找到对应的保洁任务或房间，请到系统里核对")

    name = result.get("room_name") or "房间"
    # 保洁验完房无损坏 → 提醒前台退押金（王总 2026-07-08）。仅首次完工且有
    # 关联订单时触发——task 未命中时 already_done 恒 False，若不加 has_order
    # 闸，每次点击/飞书重试都会重发一张「请核对订单」空卡刷屏退押金群。
    if not result.get("already_done") and result.get("has_order"):
        background_tasks.add_task(notify_deposit_best_effort, name, result)
    # 完成现在是在私信密码卡上点的 → 把被点的那张私信卡也改灰（群卡已由
    # complete_cleaning_for_task 里的 grey_cleaning_card_best_effort 用存的
    # message_id 改灰）。仅首次完工触发，避免重复 PATCH。
    if message_id and not result.get("already_done"):
        background_tasks.add_task(
            _grey_clicked_card_bg, message_id, name, result.get("trial_tag")
        )
    return build_toast_response(f"✅ {name}已标记打扫完成，房间已可用")


async def _handle_clean_start(
    db: AsyncSession, value: dict, open_id: str, message_id: str,
    background_tasks: BackgroundTasks,
) -> dict:
    """退房打扫卡【打扫卫生】：门闩 + 领码。被拦→提示先完成上一间；放行→
    应答后下码+私聊发密码+把群卡 PATCH 成打扫中（守 3s 红线）。"""
    result = await start_cleaning_for_task(
        db, task_id=value.get("task_id"), room_id=value.get("room_id"),
        operator_open_id=open_id,
    )
    if not result.get("task_found"):
        return build_toast_response("⚠️ 未找到对应的保洁任务，请到系统里核对", toast_type="error")
    if result.get("blocked"):
        busy = result.get("busy_room_name") or "上一间"
        return build_toast_response(
            f"⚠️ 请先完成 {busy} 的打扫，再开始下一间", toast_type="error"
        )
    # 幂等：仅首次领取才下码+私发。飞书回调至少投递一次，重试/重复点若不挡，
    # 会重复下码 + 发第二张私聊密码卡 + 多写审计（评审 P2）。
    if not result.get("already_started"):
        background_tasks.add_task(
            _clean_start_fanout_bg,
            value.get("task_id") or "", result.get("room_id") or "",
            result.get("order_id"), open_id, result.get("room_name") or "",
            message_id,
        )
    return build_toast_response("✅ 门锁密码马上私信发给你，扫完点【打扫完成】")


async def _clean_start_fanout_bg(
    task_id: str, room_id: str, order_id: str | None, requester_open_id: str,
    room_name: str, message_id: str,
) -> None:
    """领取后台扇出：下保洁码（幂等）→ 私聊发密码 → 群卡 PATCH 成打扫中。best-effort。"""
    try:
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.models.order import Order
        from app.core.trial_tags import trial_tag_for_notes
        from app.services.feishu_lead_alert import (
            send_checkout_password_to_cleaner,
            update_checkout_cleaning_inprogress,
        )
        from app.services.lock.hooks import issue_cleaning_codes_on_checkout

        async with AsyncSessionLocal() as session:
            guest_name = ""
            code = None
            trial_tag = None
            if order_id:
                order = (await session.execute(
                    select(Order).where(Order.order_id == order_id)
                )).scalar_one_or_none()
                if order is not None:
                    guest_name = order.guest_name or ""
                    trial_tag = trial_tag_for_notes(order.notes)
                    if room_id:
                        codes = await issue_cleaning_codes_on_checkout(session, order, [room_id])
                        code = codes.get(room_id)
            await send_checkout_password_to_cleaner(
                open_id=requester_open_id, room_name=room_name,
                guest_name=guest_name, cleaning_code=code,
                room_id=room_id, task_id=task_id, trial_tag=trial_tag,
            )
            await update_checkout_cleaning_inprogress(
                message_id=message_id, room_id=room_id, room_name=room_name,
                cleaner_hint="", trial_tag=trial_tag,
            )
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("退房打扫领取扇出失败 task=%s room=%s", task_id, room_id, exc_info=True)
        # 扇出挂了 → 保洁「已开始但没码」且被门闩锁死在别的房外。清掉门闩标记，
        # 让他重点【打扫卫生】重试，别把人卡死一整天（评审 P1）。
        await _reset_started_bg(task_id)


async def _reset_started_bg(task_id: str) -> None:
    """新开会话清掉领取门闩标记（扇出失败兜底）。best-effort：失败只 log。"""
    try:
        from app.core.database import AsyncSessionLocal
        from app.services.cleaning import reset_cleaning_started

        async with AsyncSessionLocal() as session:
            await reset_cleaning_started(session, task_id=task_id)
    except Exception:  # noqa: BLE001 — 尽力而为
        logger.warning("清领取门闩标记失败 task=%s", task_id, exc_info=True)


async def _grey_clicked_card_bg(
    message_id: str, room_name: str, trial_tag: str | None = None,
) -> None:
    """把被点的私信密码卡改灰（best-effort，与群卡改灰互不影响）。"""
    try:
        from app.services.feishu_lead_alert import grey_cleaning_password_card

        await grey_cleaning_password_card(
            message_id=message_id, room_name=room_name, trial_tag=trial_tag,
        )
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("退房完成改灰私信卡失败 msg=%s", message_id, exc_info=True)


async def _handle_cleaning_request_action(
    db: AsyncSession, value: dict, open_id: str, message_id: str,
    background_tasks: BackgroundTasks,
) -> dict:
    """飞书「打扫申请群」三按钮编排（cr_apply / cr_done / cr_approve）。

    - cr_apply：保洁点【申请打扫】→ 建申请（幂等）+ 应答后下码+发密码卡（新会话、best-effort）。
    - cr_done：保洁点【打扫完了】→ 标 cleaned + 应答后撤码（守 3s 红线）。
    - cr_approve：前台/管家点【通过】→ 白名单校验 → 建保洁费用(30，房东全担)。
    红线：全程绝不改房态（房里还住着人）。
    """
    from app.services.cleaning_request import (
        apply_for_cleaning,
        approve_cleaning_request,
        is_cleaning_approver,
        mark_cleaning_done,
        room_belongs_to_checked_in_order,
    )

    action = value.get("action")
    room_id = value.get("room_id") or ""
    order_id = value.get("order_id") or ""

    if action == "cr_apply":
        if not (room_id and order_id):
            return build_toast_response("⚠️ 缺少房间信息，请到系统核对")
        # 校验房确属该在住订单，再下码——挡伪造回调申请任意房门锁码（评审安全项）。
        if not await room_belongs_to_checked_in_order(db, room_id=room_id, order_id=order_id):
            return build_toast_response("⚠️ 该房不在此在住订单，无法申请，请核对", toast_type="error")
        req = await apply_for_cleaning(
            db, room_id=room_id, order_id=order_id, requester_open_id=open_id
        )
        # 下码 + 私聊发密码 + 发审核卡 + 原地更新大卡，全挂后台（锁往返最坏 ~12s，守 3s 红线）。
        background_tasks.add_task(
            _cr_apply_fanout_bg, req.request_id, room_id, order_id, open_id
        )
        return build_toast_response("✅ 已申请，门锁密码马上私信发给你")

    if action == "cr_done":
        request_id = value.get("request_id") or ""
        if not request_id:
            return build_toast_response("⚠️ 找不到该打扫申请，请重新申请")
        await mark_cleaning_done(db, request_id=request_id)
        # 撤保洁码 + 把私聊密码卡改灰，挂后台（守 3s 红线）。message_id=被点的密码卡。
        background_tasks.add_task(_cr_done_bg, room_id, order_id or None, message_id)
        return build_toast_response("✅ 已记录打扫完成，门锁密码已失效")

    # cr_approve（点击发生在审核保洁群——保洁不在此群，天然防自批）
    request_id = value.get("request_id") or ""
    if not is_cleaning_approver(open_id):
        # 纵深防御：仅当配了 open_id 白名单才会命中这里（默认靠群成员隔离，放行）。
        return build_toast_response("⚠️ 你没有审批权限，请管家/管理员操作", toast_type="error")
    if not request_id:
        return build_toast_response("⚠️ 找不到该打扫申请")
    _req, exp = await approve_cleaning_request(
        db, request_id=request_id, approver_open_id=open_id
    )
    if _req is None:
        return build_toast_response("⚠️ 找不到该打扫申请")
    # 通过后把审核卡改灰（应答后台跑）。message_id=被点的审核卡。
    background_tasks.add_task(_cr_grey_review_bg, message_id, room_id)
    if exp is not None:
        return build_toast_response("✅ 已通过")
    return build_toast_response("✅ 已通过（该房未配置业主，未计费，请核对）")


async def _cr_apply_fanout_bg(
    request_id: str, room_id: str, order_id: str, requester_open_id: str,
) -> None:
    """申请后台扇出：下保洁码 → 密码私聊发申请人 → 发审核卡到审核群 → 原地更新大卡。
    best-effort：新开会话，任何一步失败只 log，不影响已应答的申请。"""
    try:
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.core.trial_tags import trial_tag_for_notes
        from app.models.order import Order
        from app.models.room import Room
        from app.services.cleaning_request import (
            applied_room_ids_for_date,
            get_daily_card_message_id,
            list_checkout_rooms_for_cleaning,
            list_instay_rooms_for_cleaning,
        )
        from app.services.feishu_lead_alert import (
            refresh_cleaning_request_daily_card,
            send_cleaning_password_to_cleaner,
            send_cleaning_review_card,
        )
        from app.services.lock.hooks import issue_cleaning_codes_on_checkout

        async with AsyncSessionLocal() as session:
            order = (await session.execute(
                select(Order).where(Order.order_id == order_id)
            )).scalar_one_or_none()
            room = (await session.execute(
                select(Room).where(Room.room_id == room_id)
            )).scalar_one_or_none()
            room_name = room.room_name if room is not None else room_id
            guest_name = order.guest_name if order is not None else ""
            trial_tag = trial_tag_for_notes(order.notes) if order is not None else None

            code = None
            if order is not None:
                # 给在住房按需下一把保洁码（与客人码共存，幂等）。fail-safe：失败→None。
                codes = await issue_cleaning_codes_on_checkout(session, order, [room_id])
                code = codes.get(room_id)

            # 密码私聊发申请人本人（永不被群消息冲走）；私聊失败自动回退发群。
            await send_cleaning_password_to_cleaner(
                open_id=requester_open_id, room_name=room_name, guest_name=guest_name,
                cleaning_code=code, request_id=request_id, room_id=room_id, order_id=order_id,
                trial_tag=trial_tag,
            )
            # 发审核卡到审核保洁群（管家点通过=计费）。
            await send_cleaning_review_card(
                room_name=room_name, guest_name=guest_name,
                request_id=request_id, room_id=room_id, order_id=order_id,
            )
            # 原地更新每日大卡（把已申请的房标灰）。
            mid = await get_daily_card_message_id(session)
            if mid:
                rooms = await list_instay_rooms_for_cleaning(session)
                checkout_rooms = await list_checkout_rooms_for_cleaning(session)
                applied = await applied_room_ids_for_date(session)
                await refresh_cleaning_request_daily_card(
                    message_id=mid, rooms=rooms, applied_room_ids=applied,
                    checkout_rooms=checkout_rooms,
                )
    except Exception:  # noqa: BLE001 — best-effort，绝不影响已应答的申请
        logger.warning("打扫申请扇出失败 request=%s room=%s", request_id, room_id, exc_info=True)


async def _cr_done_bg(room_id: str, order_id: str | None, message_id: str) -> None:
    """打扫完了后台：撤保洁码 + 把私聊密码卡改灰。best-effort：新开会话，失败只 log。"""
    try:
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.core.trial_tags import trial_tag_for_notes
        from app.models.order import Order
        from app.models.room import Room
        from app.services.feishu_lead_alert import grey_cleaning_password_card
        from app.services.lock.hooks import revoke_cleaning_code_on_done

        async with AsyncSessionLocal() as session:
            if room_id:
                await revoke_cleaning_code_on_done(session, room_id, order_id=order_id)
                room = (await session.execute(
                    select(Room).where(Room.room_id == room_id)
                )).scalar_one_or_none()
                trial_tag = None
                if order_id:
                    order = (await session.execute(
                        select(Order).where(Order.order_id == order_id)
                    )).scalar_one_or_none()
                    trial_tag = trial_tag_for_notes(order.notes) if order is not None else None
                await grey_cleaning_password_card(
                    message_id=message_id,
                    room_name=(room.room_name if room is not None else room_id),
                    trial_tag=trial_tag,
                )
    except Exception:  # noqa: BLE001 — 尽力而为
        logger.warning("打扫完了撤码/改灰失败 room=%s", room_id, exc_info=True)


async def _cr_grey_review_bg(message_id: str, room_id: str) -> None:
    """通过后台：把审核卡改灰。best-effort：新开会话取房名，失败只 log。"""
    try:
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.models.room import Room
        from app.services.feishu_lead_alert import grey_cleaning_review_card

        async with AsyncSessionLocal() as session:
            room = (await session.execute(
                select(Room).where(Room.room_id == room_id)
            )).scalar_one_or_none() if room_id else None
            await grey_cleaning_review_card(
                message_id=message_id,
                room_name=(room.room_name if room is not None else (room_id or "房间")),
            )
    except Exception:  # noqa: BLE001 — 尽力而为
        logger.warning("审核卡改灰失败 room=%s", room_id, exc_info=True)


async def notify_deposit_best_effort(room_name: str, result: dict) -> None:
    """保洁完工后推一条「查房 + 退押金」卡到退押金群（王总 2026-07-12）。

    卡上显示一把查房门锁码（前台拿它开门查房），前台查完点「查房通过·退押金」→
    卡改灰 + 查房码失效（防重复查房）。

    硬约束：best-effort。完工回写是核心动作，提醒是附属——构造/下码/发送失败一律只
    记日志，绝不抛异常、绝不影响给保洁的完工应答。整个函数体裹在 try/except 里。
    """
    try:
        from app.services.feishu_lead_alert import (
            _deposit_app_mode_configured,
            send_deposit_alert,
            send_inspect_deposit_card,
        )

        guest = result.get("guest_name")
        deposit = result.get("deposit")
        room_id = result.get("room_id")
        order_id = result.get("order_id")

        # 发卡前下一把查房码（keeper），塞进卡片给前台开门查房。fail-safe：门锁失败
        # 返回 None → 卡上回退「暂无请联系前台」，绝不挡卡发送。
        inspect_code = await _issue_inspection_code_best_effort(order_id, room_id)

        # 本单前台存过的押金小票图/备注 → 显示进退押金卡（有图内嵌图，无图显示备注）。
        # best-effort：取不到就不显示（不影响卡发送）。
        image_keys, notes = await _deposit_receipt_media_best_effort(order_id)

        if _deposit_app_mode_configured():
            # 应用模式：发两个独立按钮卡「查房完成」「退押金」（各自打勾，两个都点完变灰）。
            await send_inspect_deposit_card(
                room=room_name, guest=guest or "",
                room_id=room_id or "", order_id=order_id or "",
                inspect_code=inspect_code, image_keys=image_keys, notes=notes,
            )
        else:
            # webhook 降级：无回传通道，退回单个跳转按钮卡（老行为）。
            lines = [f"{room_name} 已打扫完成，请查房并退押金"
                     + (f"（客人：{guest}）" if guest else "")]
            lines.append(
                f"· 查房门锁密码：{inspect_code}" if inspect_code
                else "· 查房门锁密码：暂无，请联系前台"
            )
            await send_deposit_alert(
                "🔑 查房 + 退押金", lines,
                button_text="去处理",
                button_url=f"{settings.FRONTEND_BASE_URL}/orders",
                image_keys=image_keys,
            )
    except Exception:  # noqa: BLE001 — best-effort，绝不阻断完工应答
        logger.warning("退押金提醒推送失败 room=%s", room_name, exc_info=True)


async def _deposit_receipt_media_best_effort(
    order_id: str | None,
) -> tuple[list[str], list[str]]:
    """取本单押金存档的（飞书 image_key 列表, 备注文本列表），供退押金卡显示。
    best-effort：无 order_id / 任何异常 → ([], [])（卡片降级）。"""
    if not order_id:
        return [], []
    try:
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.models.order import Order

        async with AsyncSessionLocal() as session:
            order = (await session.execute(
                select(Order).where(Order.order_id == order_id)
            )).scalar_one_or_none()
            if order is None:
                return [], []
            receipts = (order.metadata_ or {}).get("deposit_receipts", [])
            image_keys = [r["image_key"] for r in receipts
                          if isinstance(r, dict) and r.get("image_key")]
            notes = [r["note"] for r in receipts
                     if isinstance(r, dict) and r.get("note")]
            return image_keys, notes
    except Exception:  # noqa: BLE001 — best-effort，绝不阻断发卡
        logger.warning("取押金存档失败 order=%s", order_id, exc_info=True)
        return [], []


async def _issue_inspection_code_best_effort(
    order_id: str | None, room_id: str | None,
) -> str | None:
    """发退押金卡前下一把查房码，返回明文供卡片展示。best-effort：任何异常/失败→None。

    本函数在完工应答后台任务里跑，请求会话已关，故新开独立会话（同 _revoke_*_bg 模式）。
    """
    if not (order_id and room_id):
        return None
    try:
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.models.order import Order
        from app.services.lock.hooks import issue_inspection_code_on_cleaning_done

        async with AsyncSessionLocal() as session:
            order = (await session.execute(
                select(Order).where(Order.order_id == order_id)
            )).scalar_one_or_none()
            if order is None:
                return None
            codes = await issue_inspection_code_on_cleaning_done(session, order, [room_id])
            return codes.get(room_id)
    except Exception:  # noqa: BLE001 — best-effort，绝不阻断发卡
        logger.warning("下查房码失败 order=%s room=%s", order_id, room_id, exc_info=True)
        return None


async def _revoke_inspection_code_bg(room_id: str, order_id: str | None) -> None:
    """应答后撤查房码：请求会话在后台任务执行前已被 FastAPI 关闭，须新开会话。
    fail-safe：任何异常只记日志（hook 内部本就吞异常，这里兜的是建会话本身）。"""
    try:
        from app.core.database import AsyncSessionLocal
        from app.services.lock.hooks import revoke_inspection_code_on_done

        async with AsyncSessionLocal() as session:
            await revoke_inspection_code_on_done(session, room_id, order_id=order_id)
    except Exception:  # noqa: BLE001 — 尽力而为，绝不影响已应答的退押金回执
        logger.warning("后台撤查房码失败 room=%s", room_id, exc_info=True)


async def _handle_inspect_deposit(
    db: AsyncSession, value: dict, open_id: str, message_id: str,
    background_tasks: BackgroundTasks,
) -> dict:
    """两按钮退押金卡：点「查房完成」或「退押金」。

    - 点「查房完成」：撤该房查房码（防重复查房）+ 原地把卡改成「已查房」态（另留退押金按钮）。
    - 点「退押金」：原地把卡改成「已退押金」态（另留查房按钮）。
    - 两个都点过 → 整卡变灰「已查房·已退押金」。

    两次点击间的状态靠按钮 value 的字符串标志 1/0 传递（不落库）。只打勾做记录，
    绝不动订单金额/状态（退押金实退仍走 POS + 订单页，王总 2026-07-12）。
    改卡/撤码/审计都是 best-effort，慢外呼挂 background_tasks 应答后跑（守飞书 3s 红线）。
    """
    action = value.get("action")
    room = value.get("room") or "房间"
    guest = value.get("guest") or ""
    room_id = value.get("room_id") or ""
    order_id = value.get("order_id") or ""

    if action == "inspect_done":
        inspect_done, refund_done = True, value.get("refund_done") == "1"
        # 查房通过 → 撤该房查房码（防重复查房）。挂后台（调锁最坏 ~12s，守 3s 红线）。
        if room_id:
            background_tasks.add_task(_revoke_inspection_code_bg, room_id, order_id or None)
        audit_action, note, toast = ("inspect.done_feishu", "查房完成", f"✅ {room} 已记录查房完成")
    else:  # deposit_refunded
        inspect_done, refund_done = value.get("inspect_done") == "1", True
        audit_action, note, toast = ("deposit.refunded_feishu", "退押金", f"✅ {room} 已记录退押金")

    # 原地改卡（重取小票图 + 新状态重渲染）。挂后台跑（改卡最坏 ~10s，守 3s 红线）。
    background_tasks.add_task(
        _rerender_inspect_deposit_bg, message_id, room, guest, room_id, order_id,
        inspect_done, refund_done,
    )
    # 查房 + 退押金两步都点完 → 自动完成订单并释放房间（王总 2026-07-14）。
    # 办理退房/打扫/查房/退押金都不推 completed，甘特订单条与房间格会一直挂着
    # pending_checkout，此处补上自动收尾。挂后台跑（best-effort，不阻塞卡片应答）。
    if inspect_done and refund_done and order_id:
        background_tasks.add_task(_auto_complete_after_inspect_deposit_bg, order_id)
    try:
        await log_action(
            db, None, audit_action, "order", order_id or None,
            notes=f"{note} open_id={open_id} room={room}",
        )
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("%s 回执审计失败 room=%s", note, room, exc_info=True)
    return build_toast_response(toast)


async def _auto_complete_after_inspect_deposit_bg(order_id: str) -> None:
    """查房 + 退押金双完成后自动把退房单 pending_checkout→completed（新开无关会话）。

    best-effort：仅当订单仍处 pending_checkout 时推进（complete_checkout_order 内收口
    幂等/守卫）；任何异常只 log，绝不影响卡片应答或飞书重试。完成后与订单页「完成订单」
    同口径清理押金小票 OSS 图。
    """
    try:
        from app.core.audit import log_action_tx
        from app.core.database import AsyncSessionLocal
        from app.services.order_state import complete_checkout_order

        async with AsyncSessionLocal() as db:
            done = await complete_checkout_order(db, order_id)
            if not done:
                return
            await log_action_tx(
                db, None, "order.auto_completed_checkout", "order", order_id,
                notes="查房+退押金完成，自动完成订单并释放房间",
            )
            await db.commit()
        # 完成 → 清理押金小票图（与订单页完成订单同口径）。独立 best-effort。
        try:
            from app.services.deposit_receipt_cleanup import cleanup_deposit_receipt_images

            await cleanup_deposit_receipt_images(order_id)
        except Exception:  # noqa: BLE001 — best-effort
            logger.warning("自动完成后清理押金小票图失败 order=%s", order_id, exc_info=True)
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("查房退押金双完成后自动完成订单失败 order=%s", order_id, exc_info=True)


async def _rerender_inspect_deposit_bg(
    message_id: str, room: str, guest: str, room_id: str, order_id: str,
    inspect_done: bool, refund_done: bool,
) -> None:
    """应答后原地重渲染两按钮退押金卡（新开无关会话）。best-effort：任何异常只 log。"""
    try:
        image_keys, notes = await _deposit_receipt_media_best_effort(order_id)
        from app.services.feishu_lead_alert import rerender_inspect_deposit_card

        await rerender_inspect_deposit_card(
            message_id=message_id, room=room, guest=guest,
            room_id=room_id, order_id=order_id, image_keys=image_keys, notes=notes,
            inspect_done=inspect_done, refund_done=refund_done,
        )
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("重渲染查房退押金卡失败 room=%s", room, exc_info=True)


async def _handle_deposit_done(
    db: AsyncSession, value: dict, open_id: str, message_id: str,
    background_tasks: BackgroundTasks,
) -> dict:
    """点「查房通过·退押金」→ 把点中的卡改灰 + 撤该房查房码（防重复查房）+ 记审计。
    不改订单任何金额/状态（退押金实退仍走 POS 机 + 订单页，扣款人工处理，王总 2026-07-12）。

    改灰与审计都是 best-effort：失败只 log，仍回成功 toast（前台已经点了，
    别让 UI 报错吓人；改灰失败最坏是卡没变灰，可接受）。改灰要现取 token +
    PATCH（httpx，最坏 ~10s），挂 background_tasks 应答后跑——守住飞书 3s 红线，
    别让慢外呼在 HTTP 回退入口顶破窗口触发飞书重试（与 clean_done 的退押金外呼同款）。
    """
    room = value.get("room") or "房间"
    room_id = value.get("room_id") or ""
    order_id = value.get("order_id") or ""
    background_tasks.add_task(
        _grey_deposit_card_best_effort, message_id,
        value.get("room") or "", value.get("guest") or "",
    )
    # 查房通过 → 撤该房查房码（防重复查房）。挂 background_tasks 应答后跑（调锁供应商
    # 最坏 ~12s，守飞书 3s 红线）。room_id 缺失（老卡/webhook 降级）时静默跳过。
    if room_id:
        background_tasks.add_task(_revoke_inspection_code_bg, room_id, order_id or None)
    try:
        await log_action(
            db, None, "deposit.acknowledged_feishu", "order", order_id or None,
            notes=f"查房通过并退押金 open_id={open_id} room={room}",
        )
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("退押金回执审计失败 room=%s", room, exc_info=True)
    return build_toast_response(f"✅ {room}已查房并退押金")


async def _grey_deposit_card_best_effort(
    message_id: str, room_label: str, guest: str,
) -> None:
    """改灰退押金卡（应答后台跑）。best-effort：失败只 log，绝不冒泡。"""
    try:
        from app.services.feishu_lead_alert import update_deposit_card_done

        await update_deposit_card_done(
            message_id=message_id, room_label=room_label, guest=guest,
        )
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("退押金卡改灰失败 room=%s", room_label, exc_info=True)
