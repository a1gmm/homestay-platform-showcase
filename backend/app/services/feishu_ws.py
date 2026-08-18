"""飞书长连接（WebSocket）收卡片点击——绕开「飞书推送到公网地址」链路。

背景（2026-07-08 事故）：卡片回调的 HTTP 推送模式下，新旧两种订阅
（card.action.trigger / trigger_v1）配置齐全、版本发布生效后，真实点击仍从未
到达公网回调地址（飞书事件日志零投递记录、Railway 边缘日志零到达）。推送链
路无法再从配置层面定位，切换长连接：本服务进程通过 lark-oapi SDK 主动连
open.feishu.cn 收回调，不依赖任何入站可达性。

启用条件：飞书后台「事件与回调 → 回调配置 → 订阅方式」切到
「使用长连接接收回调」。切换后 HTTP 推送即停；api/v1/feishu_callback 的
HTTP 入口保留（challenge/回滚兜底），两个入口共用
services/feishu_card_actions.process_card_action 编排。

运行形态：FastAPI lifespan 起一个 daemon 线程跑 ws.Client.start()
（阻塞、SDK 自动重连），并把主 event loop 记下来。每个点击事件由 SDK 从
自己的线程/loop 同步调 handler，我们把处理协程 submit 回**主 loop** 执行——
DB 的 async engine 绑定主 loop，在别的 loop 上跑 DB 会
「Future attached to a different loop」（2026-07-08 真机点击后台报错的根因）。
"""
import asyncio
import logging
import threading

from app.core.config import settings

logger = logging.getLogger(__name__)

# FastAPI 主 event loop（DB async engine 绑定于此）。lifespan 启动时记下。
_main_loop: asyncio.AbstractEventLoop | None = None
_started = False


def set_main_loop(loop) -> None:
    """记录 FastAPI 主 event loop，供 ws 处理器把协程 submit 回来跑。"""
    global _main_loop
    _main_loop = loop


def _dispatch_to_main_loop(coro) -> str:
    """把协程丢到主 loop 后台执行（fire-and-forget，不阻塞取结果）。

    关键设计（2026-07-08）：Neon serverless 免费档首次 DB 操作可能 5-18s
    冷启动，而飞书要求卡片点击 3s 内应答——同步等 DB 必然超时。改为秒回
    确认、完工回写在主 loop 后台异步跑，彻底与 DB 速度解耦。
    主 loop 不可用时（测试 / 尚未启动）退回独立线程新 loop。
    """
    loop = _main_loop
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, loop)
        return "main-loop"
    threading.Thread(
        target=lambda: asyncio.run(coro), name="feishu-ws-handle", daemon=True,
    ).start()
    return "thread"


async def _handle_ws_click(value: dict, open_id: str, message_id: str = "") -> None:
    """后台处理卡片动作；异步失败私信点击人，异常只记日志。"""
    try:
        result = await process_ws_card_action(value, open_id, message_id)
        toast = result.get("toast") or {}
        if toast.get("type") == "error":
            await _send_ws_failure_feedback(open_id, toast.get("content") or "操作未完成")
    except Exception:  # noqa: BLE001 — 已秒回应答，后台失败只留痕
        logger.warning("飞书长连接完工处理失败", exc_info=True)


async def _send_ws_failure_feedback(open_id: str, content: str) -> None:
    """长连接已秒回后，用私聊卡把最终失败结果送达原点击人。"""
    if not open_id:
        logger.warning("飞书长连接动作失败但缺少 operator open_id: %s", content)
        return
    from app.services.feishu_lead_alert import send_card_to_user

    await send_card_to_user(
        open_id=open_id,
        card={
            "schema": "2.0",
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": "⚠️ 操作未完成"},
            },
            "body": {"elements": [{"tag": "markdown", "content": content}]},
        },
    )


async def process_ws_card_action(value: dict, open_id: str, message_id: str = "") -> dict:
    """ws 事件 → 共用编排 → toast dict。

    在主 loop 上跑（见模块头）：自建会话用主 loop 的 async engine；退押金提醒/
    撤保洁码等后置任务用 create_task 挂到**同一主 loop**，在返回应答后继续跑
    （不阻塞 toast，也不新起 loop 触发跨 loop 冲突）。
    """
    from fastapi import BackgroundTasks

    from app.core.database import AsyncSessionLocal
    from app.services.feishu_card_actions import process_card_action

    bg = BackgroundTasks()
    async with AsyncSessionLocal() as db:
        toast = await process_card_action(
            db, value=value or {}, open_id=open_id or "", background_tasks=bg,
            message_id=message_id or "",
        )
    if bg.tasks:
        # 挂到当前（主）loop，应答后继续执行。best-effort：失败只 log。
        task = asyncio.ensure_future(_run_bg_best_effort(bg))
        # 保引用防被 GC；完成即弃。
        _pending_bg.add(task)
        task.add_done_callback(_pending_bg.discard)
    return toast


_pending_bg: set = set()


async def _run_bg_best_effort(bg) -> None:
    try:
        await bg()
    except Exception:  # noqa: BLE001 — 后置任务失败绝不影响已应答的完工
        logger.warning("飞书长连接后置任务失败（撤码/退押金）", exc_info=True)


def _on_card_action_trigger(data):
    """SDK 同步回调：card.action.trigger（新版 2.0 卡片按钮点击）。"""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )

    event = getattr(data, "event", None)
    action = getattr(event, "action", None)
    operator = getattr(event, "operator", None)
    context = getattr(event, "context", None)
    value = getattr(action, "value", None) or {}
    open_id = getattr(operator, "open_id", None) or ""
    # 被点卡片的 message_id（退押金一键改灰要用：点哪张灰哪张）。
    message_id = getattr(context, "open_message_id", None) or ""
    # 秒回确认 → 不受 DB 冷启动拖累顶破飞书 3s 红线；完工回写丢主 loop 后台跑。
    _dispatch_to_main_loop(_handle_ws_click(value, open_id, message_id))
    # 秒回文案按动作区分：退押金点击不涉及房态，别回「正在更新房态」误导前台。
    ack = "已收到，正在处理" if value.get("action") == "deposit_done" \
        else "已收到，正在更新房态"
    return P2CardActionTriggerResponse(
        {"toast": {"type": "info", "content": ack}}
    )


def start_ws_client_in_thread() -> bool:
    """启动飞书长连接客户端（幂等；daemon 线程，SDK 自动重连）。

    返回是否启动。跳过条件：测试环境 / 开关关闭 / 应用凭证缺失 / SDK 未装。
    任何失败只 log，绝不阻断 FastAPI 启动。
    """
    global _started
    if _started:
        return True
    if settings.APP_ENV == "test" or not settings.FEISHU_WS_CARD_ENABLED:
        return False
    if not (settings.FEISHU_APP_ID and settings.FEISHU_APP_SECRET):
        return False
    try:
        import lark_oapi as lark
    except ImportError:
        logger.warning("lark-oapi 未安装，飞书长连接不可用（pip install lark-oapi）")
        return False

    handler = (
        lark.EventDispatcherHandler.builder(
            settings.FEISHU_CARD_VERIFICATION_TOKEN or "", "",
        )
        .register_p2_card_action_trigger(_on_card_action_trigger)
        .build()
    )
    client = lark.ws.Client(
        settings.FEISHU_APP_ID,
        settings.FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    def _run():
        try:
            client.start()  # 阻塞 + 自动重连
        except Exception:  # noqa: BLE001
            logger.warning("飞书长连接客户端退出", exc_info=True)

    threading.Thread(target=_run, name="feishu-ws", daemon=True).start()
    _started = True
    logger.info("飞书长连接客户端已启动（卡片回传交互）")
    return True
