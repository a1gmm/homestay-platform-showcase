"""飞书 webhook 托管线索通知。

原 services/feishu.py 已于 2026-04-26 删除，本模块是为 tuoguan 落地页
新写的独立 sender（仅复用 ota-sync 的「群机器人 webhook」配置方式）。
约定：FEISHU_LEADS_WEBHOOK_URL 未配置时跳过；任何失败只记日志——
通知是 best-effort，绝不能阻断留资落库。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


def _title_with_trial(title: str, trial_tag: str | None) -> str:
    """试运营房型（当前=极海）角标：命中就在卡标题后缀 ` · 🌊{tag}`，让保洁一眼
    看出这是试运营房型的单，与房态页金色小标同源（app.core.trial_tags）。空/None 不动。"""
    return f"{title} · 🌊{trial_tag}" if trial_tag else title


def _sign(timestamp: str, secret: str) -> str:
    """飞书/Lark 群机器人签名校验：以 `{timestamp}\\n{secret}` 为 HMAC-SHA256 的
    key 对空串取摘要，base64 编码。算法见飞书自定义机器人「签名校验」文档。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _sanitize(value: str) -> str:
    """用户输入会逐行拼进飞书 text 消息——去掉所有换行类字符，防止伪造字段行
    （消息注入：如 property_location 里塞 \\n电话：攻击者号码 诱导运营回拨）。
    含 Unicode 行/段分隔符 U+2028/U+2029（部分客户端会渲染为换行）。"""
    for ch in ("\r", "\n", " ", " "):
        value = value.replace(ch, " ")
    return value


def _md_safe(value: str) -> str:
    """卡片 markdown 字段防注入：去换行 + 去掉 markdown 链接/at 语法字符。

    guest_name 来自 OTA 下发（攻击者可控），直插 lark_md 会被渲染成 [文字](钓鱼链接)
    或 <at> 标签。这里连带去掉 []()<> 以免卡片里出现伪造链接/@。"""
    value = _sanitize(value or "")
    for ch in ("[", "]", "(", ")", "<", ">"):
        value = value.replace(ch, " ")
    return value


async def _post_to_feishu(text: str, *, url: str, secret: str) -> None:
    """统一发送核心：把纯文本发到指定飞书群机器人 webhook。

    url/secret 作参数传入，使线索群 / 运维待办群等多个群复用同一套
    发送 + 签名 + 错误处理逻辑（DRY）。约定不变：
    - url 为空 → 优雅降级，直接跳过（只在调用方有需要时 log）。
    - 任何 HTTP / 业务错误只记 warning，绝不向上抛出（best-effort，不阻断主流程）。
    """
    await _deliver({"msg_type": "text", "content": {"text": text}}, url=url, secret=secret)


async def _post_card_to_feishu(card: dict, *, url: str, secret: str) -> None:
    """卡片发送支路（#117）：把飞书 interactive 卡片发到指定群机器人 webhook。

    与 _post_to_feishu 共用同一套签名 / 错误处理 / 优雅降级 / best-effort 约定，
    只是 payload 换成 {"msg_type": "interactive", "card": <card>}。
    """
    await _deliver({"msg_type": "interactive", "card": card}, url=url, secret=secret)


# 跨海（Railway 美区 → open.feishu.cn）偶发超时/连接中断值得重试；这类传输层错误
# 通常意味着请求没走完一个来回，飞书大概率没处理 → 重试不会重复发。业务错误（HTTP
# 200 + code!=0：关键词/签名/token 不匹配）是确定性的，重试无用，只发一次。
_RETRYABLE = (httpx.TimeoutException, httpx.TransportError)
_MAX_ATTEMPTS = 3


async def _deliver(payload: dict, *, url: str, secret: str) -> bool:
    """文本与卡片共用的实际投递核心：补签名、POST、检查 code、吞异常。返回是否送达。

    payload 由调用方按消息类型构造好（text 或 interactive / image）。本函数只负责
    签名 + 发送 + 错误处理，使各类消息共用同一套逻辑（DRY，签名只一份）。

    返回 True 仅当飞书接受（HTTP 200 + code 0）；否则 False（供上层决定是否告警/补发）。
    传输层错误（跨海超时/连接中断）重试至多 _MAX_ATTEMPTS 次，退避 0.5s/1.5s。绝不抛。
    """
    if not url:
        return False
    if secret:
        # 机器人开了签名校验：带上 timestamp + sign，否则飞书拒收（code 19021）。
        # 重试均在数秒内完成，远小于飞书签名有效窗口，故 timestamp 只算一次。
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = _sign(timestamp, secret)
    last = "unknown"
    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "Feishu alert non-200: %s %s",
                    resp.status_code, resp.text[:200],
                )
                return False  # 非 200（多为网关/权限），重试无用
            # 飞书群机器人对 token 失效/签名失败/关键词不匹配等业务错误
            # 返回 HTTP 200 + body 内 {"code": 非0, "msg": ...}，需单独检查。
            try:
                body = resp.json()
            except Exception:
                logger.warning("Feishu alert: unparseable response body")
                return False
            if body.get("code"):
                logger.warning(
                    "Feishu alert rejected: code=%s msg=%s",
                    body.get("code"), str(body.get("msg"))[:100],
                )
                return False  # 确定性业务错误，重试无用
            return True
        except _RETRYABLE as e:
            last = type(e).__name__
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.5 + attempt)  # 0.5s, 1.5s
                continue
        except Exception as e:
            # 异常文本里若出现 webhook URL（URL 即密钥，httpx 部分异常类会内嵌），
            # 先替换再截断，类型名保留可观测性。非传输类异常不重试。
            msg = str(e).replace(url, "<webhook-url>")[:120]
            logger.warning("Feishu alert failed: %s: %s", type(e).__name__, msg)
            return False
    logger.warning("Feishu alert failed after %d attempts: %s", _MAX_ATTEMPTS, last)
    return False


async def send_lead_alert(name: str, phone: str, property_location: str) -> None:
    """新线索逐条通知 → 线索群（LEADS）。phone 已被 schema 正则约束为纯数字。"""
    await send_ops_alert(
        "🏠 新托管线索（观海居落地页）\n"
        f"称呼：{_sanitize(name)}\n"
        f"电话：{phone}\n"
        f"房源位置：{_sanitize(property_location)}\n"
        "请 24 小时内联系业主"
    )


async def send_ops_alert(text: str) -> None:
    """发送任意文本到线索群（LEADS）。线索逐条通知与线索域运维告警（如 flood
    护栏）共用此通道——受众都是盯线索的老板。订单提醒（逾期/明日入住）请改用
    send_checkin_alert 发到订单提醒群。"""
    await _post_to_feishu(
        text,
        url=settings.FEISHU_LEADS_WEBHOOK_URL,
        secret=settings.FEISHU_LEADS_WEBHOOK_SECRET,
    )


async def send_password_text(text: str) -> None:
    """发送门锁客人入住卡片纯文本到密码群（PASSWORD）。走纯文本通道——方便前台
    整条选中复制转发给客人（交互卡片带表头/按钮不便逐字复制）。密码群只发密码
    （王总 2026-07-08 拆群）。未配置 PASSWORD webhook 则跳过（优雅降级）。"""
    await _post_to_feishu(
        text,
        url=settings.FEISHU_PASSWORD_WEBHOOK_URL,
        secret=settings.FEISHU_PASSWORD_WEBHOOK_SECRET,
    )


async def send_password_image(image_key: str, room_label: str | None = None) -> None:
    """把一张已上传飞书的图片（image_key）发到密码群（PASSWORD）。
    用于办理入住时在门锁密码卡片后紧跟该房间的入住登记二维码。image_key 由自建应用
    预先经 im/v1/images 上传得到（见 services/lock/checkin_qr.py）。best-effort：
    未配置 webhook / 任何错误只 log，绝不抛（图片只是加分项，不能影响卡片与办入住）。

    _deliver 已对跨海超时重试；仍最终失败时告警到门锁群，前台可据 image_key 手动补发
    ——历史上这条发图失败是完全静默的，一次瞬时抖动就永久丢图且无人知晓（2026-07-19）。"""
    if not image_key:
        return
    ok = await _deliver(
        {"msg_type": "image", "content": {"image_key": image_key}},
        url=settings.FEISHU_PASSWORD_WEBHOOK_URL,
        secret=settings.FEISHU_PASSWORD_WEBHOOK_SECRET,
    )
    if not ok:
        where = f"（{room_label}）" if room_label else ""
        try:
            await send_lock_alert(
                f"⚠️ 入住登记二维码{where}未发到密码群，请手动补发。\n"
                f"image_key={image_key}"
            )
        except Exception:  # noqa: BLE001 — 告警本身失败也不得影响主流程
            logger.warning("QR image failure alert itself failed")


async def send_password_card(card: dict) -> None:
    """把一张 interactive 卡片发到密码群（PASSWORD）。用于入住时在密码卡后紧跟一张
    「上传押金小票」按钮卡（按钮=打开链接，不需要回调/自建应用）。best-effort：
    未配置 webhook / 任何错误只 log，绝不抛（按钮卡是附属，不能影响入住主流程）。"""
    if not card:
        return
    await _post_card_to_feishu(
        card,
        url=settings.FEISHU_PASSWORD_WEBHOOK_URL,
        secret=settings.FEISHU_PASSWORD_WEBHOOK_SECRET,
    )


async def send_lock_battery_alert(text: str) -> None:
    """发送门锁低电告警到门锁告警群（LOCK_ALERT）。一类一群（王总规则）。
    未配置 LOCK_ALERT webhook 则跳过（优雅降级，只 log）。"""
    await _post_to_feishu(
        text,
        url=settings.FEISHU_LOCK_ALERT_WEBHOOK_URL,
        secret=settings.FEISHU_LOCK_ALERT_WEBHOOK_SECRET,
    )


async def send_sync_alert(text: str) -> None:
    """发送宝禹同步告警（拉取水位看门狗）到同步告警群（SYNC_ALERT——与 ota-sync 待办群
    同一机器人，Cyrus + 前台都在）。未配置则跳过（优雅降级，只 log）。"""
    await _post_to_feishu(
        text,
        url=settings.FEISHU_SYNC_ALERT_WEBHOOK_URL,
        secret=settings.FEISHU_SYNC_ALERT_WEBHOOK_SECRET,
    )


async def send_billing_recon_alert(text: str) -> None:
    """发送账单对账摘要（Task 8）到对账专用群（RECON——bill_month/行数/差异分类计数/
    批次链接文案由调用方 app/api/v1/billing_recon.py 拼好）。未配置 FEISHU_RECON_WEBHOOK_URL
    则跳过（优雅降级，只 log）。"""
    await _post_to_feishu(
        text,
        url=settings.FEISHU_RECON_WEBHOOK_URL,
        secret=settings.FEISHU_RECON_WEBHOOK_SECRET,
    )


async def send_lock_alert(text: str) -> None:
    """发送门锁运维告警（如下码失败）到门锁告警群（LOCK_ALERT）。与低电告警同群，
    受众都是盯门锁的人。未配置 LOCK_ALERT webhook 则跳过（优雅降级，只 log）。"""
    await _post_to_feishu(
        text,
        url=settings.FEISHU_LOCK_ALERT_WEBHOOK_URL,
        secret=settings.FEISHU_LOCK_ALERT_WEBHOOK_SECRET,
    )


async def _get_tenant_access_token() -> str | None:
    """用 App ID + App Secret 换 tenant_access_token（飞书自建应用发消息凭证）。

    best-effort：拿不到返回 None，调用方据此跳过。不做缓存——保洁通知低频
    （每次退房一条），每次现取最简单可靠，避免缓存过期/并发刷新的复杂度。
    """
    app_id = settings.FEISHU_APP_ID
    app_secret = settings.FEISHU_APP_SECRET
    if not (app_id and app_secret):
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            body = resp.json()
            if body.get("code") == 0:
                return body.get("tenant_access_token")
            logger.warning(
                "Feishu tenant_access_token failed: code=%s msg=%s",
                body.get("code"), str(body.get("msg"))[:100],
            )
    except Exception as e:  # noqa: BLE001 — best-effort，绝不阻断主流程
        logger.warning("Feishu tenant_access_token error: %s", type(e).__name__)
    return None


async def upload_image_to_feishu(content: bytes, content_type: str) -> str | None:
    """把图片字节上传飞书 im/v1/images，返回 image_key（用于退押金卡内嵌显示小票）。

    best-effort：拿不到 token / 上传失败一律只 log 返回 None——退房卡显示图是加分项，
    拿不到 image_key 时上传主流程照常（OSS 已存档，卡片降级为不显示图）。
    """
    if not content:
        return None
    token = await _get_tenant_access_token()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": ("receipt", content, content_type)},
            )
            body = resp.json()
            if body.get("code"):
                logger.warning(
                    "Feishu image upload rejected: code=%s msg=%s",
                    body.get("code"), str(body.get("msg"))[:100],
                )
                return None
            return (body.get("data") or {}).get("image_key")
    except Exception as e:  # noqa: BLE001 — best-effort，绝不阻断上传主流程
        logger.warning("Feishu image upload error: %s", type(e).__name__)
        return None


def _receipt_img_elements(image_keys: list[str] | None) -> list[dict]:
    """把押金小票 image_key 列表转成卡片 img 元素（1.0/2.0 卡通用同一 tag）。"""
    if not image_keys:
        return []
    return [
        {"tag": "img", "img_key": k,
         "alt": {"tag": "plain_text", "content": "押金小票"}}
        for k in image_keys if k
    ]


async def _send_interactive(receive_id: str, receive_id_type: str, card: dict) -> str | None:
    """自建应用发 interactive 卡片核心：群(chat_id)/单聊(open_id) 共用。

    best-effort：任何错误只 log 不抛。成功返回 message_id（原地改卡句柄）。

    跨海瞬时抖动重试（与 webhook 通道的 `_deliver` 同套路，退避 0.5s/1.5s）：
    传输层错误（超时/连接中断）、以及取 token 瞬时失败（None，多为跨海抖动/限流）
    都重试至多 _MAX_ATTEMPTS 次；HTTP 200 + code!=0（receive_id 非法/权限等确定性
    业务错误）不重试。历史上这条单发无重试，保洁密码私信偶发静默丢（2026-07-29）。
    """
    if not receive_id:
        return None
    last = "unknown"
    for attempt in range(_MAX_ATTEMPTS):
        token = await _get_tenant_access_token()
        if not token:
            last = "no_token"
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.5 + attempt)  # 0.5s, 1.5s
                continue
            break
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages",
                    params={"receive_id_type": receive_id_type},
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": receive_id,
                        "msg_type": "interactive",
                        "content": json.dumps(card, ensure_ascii=False),
                    },
                )
            body = resp.json()
            if body.get("code"):
                logger.warning(
                    "Feishu app send rejected: code=%s msg=%s",
                    body.get("code"), str(body.get("msg"))[:100],
                )
                return None  # 确定性业务错误，重试无用
            return (body.get("data") or {}).get("message_id")
        except _RETRYABLE as e:
            last = type(e).__name__
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.5 + attempt)  # 0.5s, 1.5s
                continue
        except Exception as e:  # noqa: BLE001 — 非传输类异常不重试
            logger.warning("Feishu app send failed: %s", type(e).__name__)
            return None
    logger.warning("Feishu app send failed after %d attempts: %s", _MAX_ATTEMPTS, last)
    return None


async def send_card_via_app(*, chat_id: str, card: dict) -> str | None:
    """把 interactive 卡片发到群 chat_id（回传交互按钮必须走应用发）。返回 message_id / None。"""
    return await _send_interactive(chat_id, "chat_id", card)


async def send_card_to_user(*, open_id: str, card: dict) -> str | None:
    """把 interactive 卡片私聊发给某飞书用户（open_id）。返回 message_id / None。

    用于把门锁密码私发给申请的保洁本人——不往群里刷，永远不会被后续消息冲下去。
    """
    return await _send_interactive(open_id, "open_id", card)


def _build_cleaning_done_card(
    *, room_id: str, room_name: str, checkout_time: str, done_time: str,
    trial_tag: str | None = None,
) -> dict:
    """完成态灰卡（方案D，视觉基准 保洁卡片设计/方案D-单卡变灰.png）。

    header grey + 一行正文 + 无按钮——群里蓝卡=没干的活、灰卡=干完的活。

    **必须 card JSON 2.0**（schema=2.0 + body.elements + markdown 正文）：生产蓝卡
    走应用模式带回调按钮，是 2.0（见 _build_card 的 callback_button 分支），飞书不允许
    用 1.0 内容 PATCH 一张 2.0 卡（真机报 code=230099 / 200830 schemaV2 can not change
    schemaV1）。灰卡必须与蓝卡同 schema 才能原地改灰。
    """
    if checkout_time:
        line = f"{checkout_time} 退房 · {done_time} 扫完，房间已可入住"
    else:
        line = f"{done_time} 扫完，房间已可入住"
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "grey",
            "title": {"tag": "plain_text",
                      "content": _title_with_trial(f"✅ 已打扫 · {room_id} {room_name}", trial_tag)},
        },
        "body": {"elements": [{"tag": "markdown", "content": line}]},
    }


async def update_cleaning_card_done(
    *, message_id: str, room_id: str, room_name: str,
    checkout_time: str, done_time: str, trial_tag: str | None = None,
) -> None:
    """把保洁群里已发的蓝卡原地改成灰色完成卡（PATCH im/v1/messages）。

    best-effort：message_id 为空（webhook 模式/存储失败）直接跳过；拿不到 token、
    飞书拒收、网络异常一律只 log——完工回写是核心动作，改卡是附属。
    需要自建应用具备「更新应用发送的消息卡片」权限（上线前已核实，见 plan Task 0）。
    """
    if not message_id:
        return
    card = _build_cleaning_done_card(
        room_id=room_id, room_name=room_name,
        checkout_time=checkout_time, done_time=done_time, trial_tag=trial_tag,
    )
    await _patch_message_card(message_id, card)


async def _patch_message_card(message_id: str, card: dict) -> None:
    """把已发的卡片原地改成新卡（PATCH im/v1/messages/{message_id}）。保洁灰卡 /
    退押金灰卡共用同一套发送核（DRY）。

    best-effort：message_id 为空直接跳过；拿不到 token、飞书拒收、网络异常一律
    只 log——完工/回执回写是核心动作，改卡是附属。需自建应用具备「更新应用发送的
    消息卡片」权限。灰卡须与原卡同 schema 2.0，否则飞书拒 230099/200830。
    """
    if not message_id:
        return
    token = await _get_tenant_access_token()
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"content": json.dumps(card, ensure_ascii=False)},
            )
            body = resp.json()
            if body.get("code"):
                logger.warning(
                    "Feishu card update rejected: code=%s msg=%s",
                    body.get("code"), str(body.get("msg"))[:100],
                )
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning("Feishu card update failed: %s", type(e).__name__)


def _build_deposit_done_card(*, room_label: str, guest: str) -> dict:
    """退押金完成态灰卡（复用保洁方案D 视觉）。header grey + 一行正文 + 无按钮——
    退押金群里蓝卡=没退、灰卡=已退。

    与生产蓝卡（应用模式回传按钮，见 _build_card 的 callback_button 分支）同 schema
    2.0，飞书才允许原地 PATCH（1.0 内容改 2.0 卡报 230099/200830）。
    """
    who = f"（{guest}）" if guest else ""
    line = f"{room_label}{who} 已查房并退押金" if room_label else "已查房并退押金"
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "grey",
            "title": {"tag": "plain_text", "content": "✅ 已查房·已退押金"},
        },
        "body": {"elements": [{"tag": "markdown", "content": line}]},
    }


async def update_deposit_card_done(
    *, message_id: str, room_label: str, guest: str = "",
) -> None:
    """把退押金群里已发的青色蓝卡原地改成灰色「✅ 已退押金」（点击回执触发）。

    message_id 来自点击回调事件自带的 open_message_id（点哪张灰哪张，无需持久化）。
    best-effort：空 message_id / 拿不到 token / 飞书拒收 / 网络异常一律只 log。
    """
    if not message_id:
        return
    card = _build_deposit_done_card(room_label=room_label, guest=guest)
    await _patch_message_card(message_id, card)


def _build_inspect_deposit_card(
    *, room: str, guest: str, room_id: str, order_id: str,
    inspect_code: str | None, image_keys: list[str] | None,
    inspect_done: bool, refund_done: bool, notes: list[str] | None = None,
) -> dict:
    """查房+退押金卡：两个独立按钮「查房完成」「退押金」。点一个原地改卡（该键变✅、另一个
    还在），两个都点完 → 灰卡。谁先点都行。两次点击之间的状态靠按钮 value 里的字符串标志
    「1/0」传递（不落库，点哪张改哪张，同现有退押金灰卡手法）。schema 2.0，飞书才允许原地 PATCH。

    这俩按钮只是「打勾做记录」——真退押金仍走 POS + 订单页，本卡不动订单任何金额/状态
    （王总 2026-07-12）。"""
    both = inspect_done and refund_done
    who = f"（客人：{guest}）" if guest else ""
    lines = [f"{room}{who}"]
    if inspect_code and not inspect_done:
        lines.append(f"查房门锁密码：{inspect_code}")
    # 前台手打的备注（没拍小票时的兜底记录）——去掉 markdown 注入字符后逐条显示。
    for n in (notes or []):
        lines.append(f"📝 备注：{_md_safe(n)}")
    elements: list[dict] = [{"tag": "markdown", "content": "\n".join(lines)}]
    # 小票图只在「押金还没退」时显示——退押金要扫它;退完就没用了,隐藏免占屏。
    if not refund_done:
        elements += _receipt_img_elements(image_keys)
    elements.append({"tag": "hr"})
    if both:
        elements.append({"tag": "markdown", "content": "✅ 已查房完成 · 已退押金"})
    else:
        base = {"room": room, "guest": guest, "room_id": room_id, "order_id": order_id}
        if inspect_done:
            elements.append({"tag": "markdown", "content": "✅ 已查房完成"})
        else:
            elements.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查房完成"},
                "type": "primary",
                "behaviors": [{"type": "callback", "value": {
                    **base, "action": "inspect_done",
                    "refund_done": "1" if refund_done else "0"}}],
            })
        if refund_done:
            elements.append({"tag": "markdown", "content": "✅ 已退押金"})
        else:
            elements.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "退押金"},
                "type": "default",
                "behaviors": [{"type": "callback", "value": {
                    **base, "action": "deposit_refunded",
                    "inspect_done": "1" if inspect_done else "0"}}],
            })
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "grey" if both else "turquoise",
            "title": {"tag": "plain_text",
                      "content": "✅ 已查房 · 已退押金" if both else "🔑 查房 + 退押金"},
        },
        "body": {"elements": elements},
    }


async def send_inspect_deposit_card(
    *, room: str, guest: str, room_id: str, order_id: str,
    inspect_code: str | None, image_keys: list[str] | None,
    notes: list[str] | None = None,
) -> str | None:
    """发「查房+退押金」两按钮卡到退押金群（应用模式）。返回 message_id / None。"""
    card = _build_inspect_deposit_card(
        room=room, guest=guest, room_id=room_id, order_id=order_id,
        inspect_code=inspect_code, image_keys=image_keys, notes=notes,
        inspect_done=False, refund_done=False,
    )
    return await send_card_via_app(chat_id=settings.FEISHU_DEPOSIT_CHAT_ID, card=card)


async def rerender_inspect_deposit_card(
    *, message_id: str, room: str, guest: str, room_id: str, order_id: str,
    image_keys: list[str] | None, inspect_done: bool, refund_done: bool,
    notes: list[str] | None = None,
) -> None:
    """点了某按钮后原地把卡改成新状态。best-effort：空 message_id / 任何错误只 log。"""
    if not message_id:
        return
    card = _build_inspect_deposit_card(
        room=room, guest=guest, room_id=room_id, order_id=order_id,
        inspect_code=None, image_keys=image_keys, notes=notes,
        inspect_done=inspect_done, refund_done=refund_done,
    )
    await _patch_message_card(message_id, card)


def _build_card(
    *,
    title: str,
    template: str,
    fields: list[dict],
    button: dict | None = None,
    callback_button: dict | None = None,
    image_keys: list[str] | None = None,
) -> dict:
    """构造一张飞书 interactive 卡片（#117）。已在生产 code=0 渲染通过的结构。

    - title/template：表头标题与配色（blue=保洁，orange=运维待办）。
    - fields：div.fields 列表，每项形如
      {"is_short": bool, "text": {"tag": "lark_md", "content": "**标签**\\n值"}}。
      长字段（房间/房型/说明/列表）传 is_short=False，短字段 True。
    - button：可选跳转按钮 {"text": ..., "url": ...}（点击打开网页，webhook 卡片用）。
    - callback_button：可选回传按钮 {"text": ..., "value": {...}}（点击回写系统，
      自建应用卡片用；value 是开发者自定义数据，回调时原样带回）。
      button / callback_button 二选一；都无则省略 action 行。
    """
    img_elements = _receipt_img_elements(image_keys)
    elements: list[dict] = [{"tag": "div", "fields": fields}, *img_elements]
    if button:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": button["text"]},
                "url": button["url"],
                "type": "primary",
            }],
        })
    elif callback_button:
        # 回传按钮必须走卡片 JSON 2.0 + behaviors callback：1.0 结构的 value 按钮
        # 点击走「消息卡片请求网址」老管道（本应用从未配置），点击根本到不了
        # 服务器（2026-07-08 事故第二层根因）。2.0 的 behaviors 才路由到
        # 「事件与回调→回调配置」（card.action.trigger）。字段拼成 markdown 正文。
        md = "\n".join(f["text"]["content"] for f in fields)
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "body": {"elements": [
                {"tag": "markdown", "content": md},
                *img_elements,
                {"tag": "hr"},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": callback_button["text"]},
                    "type": "primary",
                    "behaviors": [{"type": "callback", "value": callback_button["value"]}],
                },
            ]},
        }
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


async def _send_list_card(
    title: str,
    lines: list[str],
    *,
    url: str,
    secret: str,
    template: str,
    button_text: str | None = None,
    button_url: str | None = None,
    image_keys: list[str] | None = None,
) -> None:
    """「表头 + 逐行明细长字段 + 可选跳转按钮」提醒卡片的统一发送核心。

    订单/退房/退押金三类提醒结构完全相同（一张长字段列出明细行），只是发往不同群 +
    表头配色不同——共用此核心，避免每类各抄一份卡片构造（DRY）。lines 为空时正文占位
    「—」。button_text/button_url 齐备才渲染按钮。image_keys 非空则在正文下内嵌图片
    （退押金卡显示押金小票）。webhook 未配置则优雅降级（只 log）。
    """
    body = "\n".join(lines) if lines else "—"
    button = None
    if button_text and button_url:
        button = {"text": button_text, "url": button_url}
    card = _build_card(
        title=title,
        template=template,
        fields=[{"is_short": False, "text": {"tag": "lark_md", "content": body}}],
        button=button,
        image_keys=image_keys,
    )
    await _post_card_to_feishu(card, url=url, secret=secret)


async def send_checkin_alert(
    title: str,
    lines: list[str],
    *,
    button_text: str | None = None,
    button_url: str | None = None,
) -> None:
    """发送入住提醒卡片到入住提醒群（CHECKIN，原名订单提醒群）。只发「明日入住」。
    2026-07-08 正名 ORDER→CHECKIN，避免与 ota-sync 的订单播报群混淆。橙色卡。
    未配置 CHECKIN webhook 则跳过（优雅降级）。"""
    await _send_list_card(
        title, lines,
        url=settings.FEISHU_CHECKIN_WEBHOOK_URL,
        secret=settings.FEISHU_CHECKIN_WEBHOOK_SECRET,
        template="orange",
        button_text=button_text, button_url=button_url,
    )


async def send_task_alert(
    title: str,
    lines: list[str],
    *,
    button_text: str | None = None,
    button_url: str | None = None,
) -> None:
    """发送任务提醒卡片到任务提醒群（TASK）。逾期任务（保洁/维修/自定义任务过截止
    未做）走这里——是「任务系统」的事、不是订单，故与入住提醒群分开（王总一类一群）。
    橙色卡。未配置 TASK webhook 则跳过（优雅降级）。"""
    await _send_list_card(
        title, lines,
        url=settings.FEISHU_TASK_WEBHOOK_URL,
        secret=settings.FEISHU_TASK_WEBHOOK_SECRET,
        template="orange",
        button_text=button_text, button_url=button_url,
    )


async def send_checkout_alert(
    title: str,
    lines: list[str],
    *,
    button_text: str | None = None,
    button_url: str | None = None,
) -> None:
    """发送退房提醒卡片到退房提醒群（CHECKOUT）。每天约 11:30 一条「哪些房还没退房」，
    前台据此催客人退房（王总 2026-07-08）。红色卡（催办紧迫感）。
    未配置 CHECKOUT webhook 则跳过（优雅降级）。"""
    await _send_list_card(
        title, lines,
        url=settings.FEISHU_CHECKOUT_WEBHOOK_URL,
        secret=settings.FEISHU_CHECKOUT_WEBHOOK_SECRET,
        template="red",
        button_text=button_text, button_url=button_url,
    )


async def send_deposit_alert(
    title: str,
    lines: list[str],
    *,
    button_text: str | None = None,
    button_url: str | None = None,
    callback_value: dict | None = None,
    image_keys: list[str] | None = None,
) -> str | None:
    """发送退押金提醒卡片到退押金群（DEPOSIT）。保洁点「打扫完了」验完房无损坏后
    一条「X 房可退押金」，前台据此退钱给客人（王总 2026-07-08）。青色卡。

    两种发送模式（与保洁卡片同）：
    - **应用模式**（三凭证 + DEPOSIT chat_id 配齐 且传了 callback_value）：由自建应用
      发送，带回传按钮「已退押金」（点击原地把卡改灰，不跳网页）。返回 message_id。
    - **webhook 模式**（默认/降级）：群机器人 webhook 发送 + 跳转按钮。返回 None。
      未配置 DEPOSIT webhook 则跳过（优雅降级）。
    """
    if _deposit_app_mode_configured() and callback_value is not None:
        card = _build_card(
            title=title,
            template="turquoise",
            fields=[{"is_short": False,
                     "text": {"tag": "lark_md", "content": "\n".join(lines) if lines else "—"}}],
            callback_button={"text": button_text or "已退押金", "value": callback_value},
            image_keys=image_keys,
        )
        return await send_card_via_app(chat_id=settings.FEISHU_DEPOSIT_CHAT_ID, card=card)

    await _send_list_card(
        title, lines,
        url=settings.FEISHU_DEPOSIT_WEBHOOK_URL,
        secret=settings.FEISHU_DEPOSIT_WEBHOOK_SECRET,
        template="turquoise",
        button_text=button_text, button_url=button_url,
        image_keys=image_keys,
    )
    return None


def _deposit_app_mode_configured() -> bool:
    """退押金卡应用模式（带「已退押金」回传按钮）是否配齐：
    App ID + App Secret + 退押金群 chat_id 三者全有才走应用发送。"""
    return bool(
        settings.FEISHU_APP_ID
        and settings.FEISHU_APP_SECRET
        and settings.FEISHU_DEPOSIT_CHAT_ID
    )


def _app_mode_configured() -> bool:
    """自建应用模式（保洁卡片带「打扫完了」回传按钮）是否配齐：
    App ID + App Secret + 保洁群 chat_id 三者全有才走应用发送。"""
    return bool(
        settings.FEISHU_APP_ID
        and settings.FEISHU_APP_SECRET
        and settings.FEISHU_CLEANING_CHAT_ID
    )


async def send_cleaning_alert(
    *,
    room_id: str,
    room_name: str,
    checkout_time: str,
    room_type: str,
    turnaround: bool = False,
    next_guest: str | None = None,
    task_id: str | None = None,
    cleaning_code: str | None = None,
    trial_tag: str | None = None,
) -> str | None:
    """发送保洁通知卡片到保洁群（CLEANING，#116/#117）。退房→脏房
    （room→pending_clean）时自动推送打扫哪间，替代微信手动喊。受众是保洁 + 孙哥。

    蓝色卡，标题「🧹 退房待打扫」，字段 房间(room_id+room_name)/退房时间/房型；
    turnaround（今日有新客）时追加优先提示长字段。

    两种发送模式：
    - **应用模式**（App ID/Secret/chat_id 配齐）：由自建应用发送，带回传按钮
      「打扫完了」（点击直接回写房态，value 带 room_id/task_id）。
    - **webhook 模式**（默认/降级）：群机器人 webhook 发送，带跳转按钮「去认领」
      →/staff/cleaner。未配置 CLEANING webhook 则跳过（优雅降级，只 log）。

    应用模式返回 message_id（完成变灰要用，见 services/cleaning.grey_cleaning_card_best_effort）；webhook 模式返回 None。
    """
    fields = [
        {"is_short": False, "text": {"tag": "lark_md",
         "content": f"**房间**\n{room_id}（{room_name}）"}},
        {"is_short": True, "text": {"tag": "lark_md",
         "content": f"**退房时间**\n{checkout_time}"}},
        {"is_short": False, "text": {"tag": "lark_md",
         "content": f"**房型**\n{room_type}"}},
    ]
    # 门锁码字段永远显示：有码给码，没码给兜底文案（暂无→让保洁一眼知道是没生成、
    # 而非系统漏发，别留空白。王总 2026-07-11 拍板方案 B）。
    code_text = cleaning_code if cleaning_code else "暂无，请联系前台"
    fields.append({"is_short": True, "text": {"tag": "lark_md",
                   "content": f"**🔑 保洁开门码**\n{code_text}"}})
    if turnaround:
        who = f"（{next_guest}）" if next_guest else ""
        fields.append({"is_short": False, "text": {"tag": "lark_md",
                       "content": f"⚡ **今日有新客{who}入住，请优先打扫**"}})

    # 今日有新客（turnaround）→ 红色表头 + 标题带优先标记，前台一眼可辨；
    # 普通退房维持蓝色。前台反馈仅正文一行「优先打扫」不够醒目（易漏看）。
    title = "🧹 退房待打扫 · ⚡今日新客优先" if turnaround else "🧹 退房待打扫"
    title = _title_with_trial(title, trial_tag)
    template = "red" if turnaround else "blue"

    if _app_mode_configured():
        # 两步自助：初始卡只放【打扫卫生】、不印密码；点了才私聊发密码 +
        # 群卡改「打扫中·无按钮」（见 feishu_card_actions._handle_clean_start）。
        card = _build_checkout_cleaning_start_card(
            room_id=room_id, room_name=room_name, checkout_time=checkout_time,
            room_type=room_type, turnaround=turnaround, next_guest=next_guest,
            task_id=task_id, trial_tag=trial_tag,
        )
        return await send_card_via_app(chat_id=settings.FEISHU_CLEANING_CHAT_ID, card=card)

    card = _build_card(
        title=title,
        template=template,
        fields=fields,
        button={
            "text": "去认领",
            "url": f"{settings.FRONTEND_BASE_URL}/staff/cleaner",
        },
    )
    await _post_card_to_feishu(
        card,
        url=settings.FEISHU_CLEANING_WEBHOOK_URL,
        secret=settings.FEISHU_CLEANING_WEBHOOK_SECRET,
    )
    return None


def _build_checkout_cleaning_start_card(
    *, room_id: str, room_name: str, checkout_time: str, room_type: str,
    turnaround: bool, next_guest: str | None, task_id: str | None,
    trial_tag: str | None = None,
) -> dict:
    """退房待打扫初始卡（schema 2.0）：房/退房时间/房型 + 【打扫卫生】按钮。
    **不印门锁码**——密码点了【打扫卫生】才私聊发给点按钮的保洁本人。"""
    lines = [
        f"**房间**\n{room_id}（{room_name}）",
        f"**退房时间**\n{checkout_time}",
        f"**房型**\n{room_type}",
    ]
    if turnaround:
        who = f"（{next_guest}）" if next_guest else ""
        lines.append(f"⚡ **今日有新客{who}入住，请优先打扫**")
    lines.append("点【打扫卫生】领取门锁密码（私信发你），扫完再点【打扫完成】。")
    title = "🧹 退房待打扫 · ⚡今日新客优先" if turnaround else "🧹 退房待打扫"
    title = _title_with_trial(title, trial_tag)
    template = "red" if turnaround else "blue"
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": [
            {"tag": "markdown", "content": "\n\n".join(lines)},
            {"tag": "hr"},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打扫卫生"},
                "type": "primary",
                "behaviors": [{"type": "callback", "value": {
                    "action": "clean_start", "room_id": room_id, "task_id": task_id or "",
                }}],
            },
        ]},
    }


def _build_checkout_cleaning_inprogress_card(
    *, room_id: str, room_name: str, cleaner_hint: str, trial_tag: str | None = None,
) -> dict:
    """打扫中群卡（schema 2.0，**无按钮**）：只提示密码已私发、去私信点完成。
    群里不留可点按钮——防保洁在一堆卡里误点别的房。"""
    hint = f"（{cleaner_hint}）" if cleaner_hint else ""
    line = f"🧹 打扫中{hint} · 门锁密码已私发到你的飞书私信，扫完在**私信**里点【打扫完成】。"
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"template": "blue", "title": {"tag": "plain_text",
                   "content": _title_with_trial(f"🧹 打扫中 · {room_id} {room_name}", trial_tag)}},
        "body": {"elements": [{"tag": "markdown", "content": line}]},
    }


async def update_checkout_cleaning_inprogress(
    *, message_id: str, room_id: str, room_name: str, cleaner_hint: str,
    trial_tag: str | None = None,
) -> None:
    """把退房待打扫初始卡原地 PATCH 成「打扫中·无按钮」态（best-effort，同 schema 2.0）。"""
    if not message_id:
        return
    await _patch_message_card(
        message_id,
        _build_checkout_cleaning_inprogress_card(
            room_id=room_id, room_name=room_name, cleaner_hint=cleaner_hint,
            trial_tag=trial_tag,
        ),
    )


def _build_checkout_password_card(
    *, room_name: str, guest_name: str, cleaning_code: str | None,
    room_id: str, task_id: str | None, trial_tag: str | None = None,
) -> dict:
    """私聊发给保洁的退房密码卡（schema 2.0）：🔑密码 + 【打扫完成】按钮（clean_done）。
    保洁全程在私信里操作：私信看码、私信点完成。完成走 complete_cleaning_for_task
    （复位房态 + 计费 + 撤码 + 群卡自动改灰，见 clean_done 分支）。"""
    code_text = cleaning_code if cleaning_code else "暂无，请联系前台"
    md = (
        f"**房间**\n{room_name}\n\n"
        f"**客人**\n{_md_safe(guest_name)}\n\n"
        f"**🔑 保洁开门码**\n{code_text}\n\n"
        f"扫完请点【打扫完成】，密码随即失效。"
    )
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"template": "blue", "title": {"tag": "plain_text",
                   "content": _title_with_trial(f"🔑 退房打扫开门码 · {room_name}", trial_tag)}},
        "body": {"elements": [
            {"tag": "markdown", "content": md},
            {"tag": "hr"},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打扫完成"},
                "type": "primary",
                "behaviors": [{"type": "callback", "value": {
                    "action": "clean_done", "room_id": room_id, "task_id": task_id or "",
                }}],
            },
        ]},
    }


async def send_checkout_password_to_cleaner(
    *, open_id: str, room_name: str, guest_name: str, cleaning_code: str | None,
    room_id: str, task_id: str | None, trial_tag: str | None = None,
) -> str | None:
    """把退房门锁密码+完成按钮私聊发给点【打扫卫生】的保洁。私聊失败回退发退房打扫群。best-effort。"""
    if not _app_mode_configured():
        return None
    card = _build_checkout_password_card(
        room_name=room_name, guest_name=guest_name, cleaning_code=cleaning_code,
        room_id=room_id, task_id=task_id, trial_tag=trial_tag,
    )
    mid = await send_card_to_user(open_id=open_id, card=card) if open_id else None
    if mid is None:
        mid = await send_card_via_app(chat_id=settings.FEISHU_CLEANING_CHAT_ID, card=card)
    return mid


def _cleaning_request_chat_id() -> str:
    """打扫申请群目标 chat_id：优先独立群，未配置回退退房保洁群（王总建群前先复用）。"""
    return settings.FEISHU_CLEANING_REQUEST_CHAT_ID or settings.FEISHU_CLEANING_CHAT_ID


def _cleaning_request_app_configured() -> bool:
    """打扫申请群自助流程（回传按钮）需要：App ID + Secret + 目标群 chat_id。"""
    return bool(
        settings.FEISHU_APP_ID
        and settings.FEISHU_APP_SECRET
        and _cleaning_request_chat_id()
    )


def _cleaning_review_chat_id() -> str:
    """审核保洁群 chat_id：优先独立审核群，未配置回退每日打扫群（未分群时先同群）。"""
    return settings.FEISHU_CLEANING_REVIEW_CHAT_ID or _cleaning_request_chat_id()


def _build_cleaning_request_daily_card(
    rooms: list[dict],
    applied_room_ids: set[str] | None = None,
    checkout_rooms: list[dict] | None = None,
) -> dict:
    """每日「打扫申请」大卡（schema 2.0，每日打扫群常驻）：逐间在住房一行，
    未申请的带【申请打扫】按钮，已申请的原地标「✅ 已申请（密码已私发）」。

    按钮 value 带 {action:"cr_apply", room_id, order_id}，点击回 cr_apply 分支
    （下码 + 密码私聊发给申请人 + 发审核卡 + 本卡原地更新）。多按钮走 2.0 body.elements。

    checkout_rooms（今日退房房）只做**提醒展示**——列房间让保洁看到今天还有哪些房要打扫，
    但**不带按钮**：它们的门锁码/计费走退房流程（查房通过计 65），不从此卡自助申请，
    以免与续住(30)串味或双下码。
    """
    applied = applied_room_ids or set()
    elements: list[dict] = [
        {"tag": "markdown",
         "content": "**今日续住房打扫**\n需要打扫的房间点【申请打扫】，门锁密码会私信发给你："}
    ]
    if not rooms:
        elements.append({"tag": "markdown", "content": "—（今日无续住在住房）"})
    for r in rooms:
        elements.append({"tag": "hr"})
        label = f"**{r.get('room_name') or r['room_id']}** · {_md_safe(r.get('guest_name') or '')}"
        # 试运营房型（极海）：一张卡列多间房，角标标在命中那一行房名后（行级），不标整卡标题。
        label = _title_with_trial(label, r.get("trial_tag"))
        if r["room_id"] in applied:
            elements.append({"tag": "markdown", "content": f"{label}\n✅ 已申请（密码已私发）"})
        else:
            elements.append({"tag": "markdown", "content": label})
            elements.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "申请打扫"},
                "type": "primary",
                "behaviors": [{"type": "callback", "value": {
                    "action": "cr_apply",
                    "room_id": r["room_id"],
                    "order_id": r["order_id"],
                }}],
            })
    # 今日退房房：纯展示区块（无按钮）。客人中午前还没走、进不去，退房时系统会另出退房打扫卡。
    if checkout_rooms:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown",
                         "content": "**今日退房房**（客人退房后打扫，无需在此申请）："})
        for r in checkout_rooms:
            label = f"· **{r.get('room_name') or r['room_id']}** · {_md_safe(r.get('guest_name') or '')}"
            label = _title_with_trial(label, r.get("trial_tag"))
            elements.append({"tag": "markdown", "content": label})
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "🧹 打扫申请（续住房）"},
        },
        "body": {"elements": elements},
    }


async def send_cleaning_request_daily_card(
    rooms: list[dict], checkout_rooms: list[dict] | None = None
) -> str | None:
    """发每日打扫申请大卡到每日打扫群（应用模式带回传按钮）。返回 message_id / None。

    checkout_rooms（今日退房房）纯展示，无按钮（见 _build_cleaning_request_daily_card）。
    应用模式未配齐时降级为无按钮 webhook 明细卡（至少让保洁看到房间，自助拿码不可用）。
    """
    if _cleaning_request_app_configured():
        card = _build_cleaning_request_daily_card(rooms, checkout_rooms=checkout_rooms)
        return await send_card_via_app(chat_id=_cleaning_request_chat_id(), card=card)
    lines = [f"· {r.get('room_name') or r['room_id']}（{r.get('guest_name') or ''}）" for r in rooms]
    for r in checkout_rooms or []:
        lines.append(f"· {r.get('room_name') or r['room_id']}（{r.get('guest_name') or ''}）· 今日退房")
    await _send_list_card(
        "🧹 打扫申请（续住房）", lines or ["—"],
        url=settings.FEISHU_CLEANING_WEBHOOK_URL,
        secret=settings.FEISHU_CLEANING_WEBHOOK_SECRET,
        template="turquoise",
    )
    return None


async def refresh_cleaning_request_daily_card(
    *, message_id: str, rooms: list[dict], applied_room_ids: set[str],
    checkout_rooms: list[dict] | None = None,
) -> None:
    """原地更新每日大卡（把已申请的房标灰、去掉其申请按钮）。best-effort：空 id/失败只 log。

    checkout_rooms 需一并传回，否则原地更新会把「今日退房房」展示块刷没。
    """
    if not message_id:
        return
    card = _build_cleaning_request_daily_card(rooms, applied_room_ids, checkout_rooms)
    await _patch_message_card(message_id, card)


def _build_cleaning_password_card(
    *, room_name: str, guest_name: str, cleaning_code: str | None,
    request_id: str, room_id: str, order_id: str, trial_tag: str | None = None,
) -> dict:
    """私聊发给保洁本人的密码卡（schema 2.0）：🔑密码 + 【打扫完了】按钮。

    打扫完了(cr_done)：保洁点 → 撤码 + 标 cleaned + 本卡改灰。私聊发 = 永不被群消息冲下去。
    """
    code_text = cleaning_code if cleaning_code else "暂无，请联系前台"
    md = (
        f"**房间**\n{room_name}\n\n"
        f"**客人**\n{_md_safe(guest_name)}\n\n"
        f"**🔑 保洁开门码**\n{code_text}\n\n"
        f"扫完请点【打扫完了】，密码随即失效。"
    )
    common = {"room_id": room_id, "order_id": order_id, "request_id": request_id}
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text",
                      "content": _title_with_trial(f"🧹 打扫开门码 · {room_name}", trial_tag)},
        },
        "body": {"elements": [
            {"tag": "markdown", "content": md},
            {"tag": "hr"},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打扫完了"},
                "type": "primary",
                "behaviors": [{"type": "callback", "value": {"action": "cr_done", **common}}],
            },
        ]},
    }


async def send_cleaning_password_to_cleaner(
    *, open_id: str, room_name: str, guest_name: str, cleaning_code: str | None,
    request_id: str, room_id: str, order_id: str, trial_tag: str | None = None,
) -> str | None:
    """把门锁密码卡私聊发给申请的保洁本人（open_id）。返回 message_id / None。

    私聊失败（用户没开机器人会话等）时回退发到每日打扫群（best-effort，密码仍能送达）。
    """
    if not _cleaning_request_app_configured():
        return None
    card = _build_cleaning_password_card(
        room_name=room_name, guest_name=guest_name, cleaning_code=cleaning_code,
        request_id=request_id, room_id=room_id, order_id=order_id, trial_tag=trial_tag,
    )
    mid = await send_card_to_user(open_id=open_id, card=card) if open_id else None
    if mid is None:
        # 私聊没送达 → 回退发群（带房号标题，保洁按房号找）。
        mid = await send_card_via_app(chat_id=_cleaning_request_chat_id(), card=card)
    return mid


def _build_cleaning_review_card(
    *, room_name: str, guest_name: str, request_id: str, room_id: str, order_id: str,
) -> dict:
    """发到审核保洁群的审核卡（schema 2.0）：房/客人 + 【通过】按钮。

    通过(cr_approve)：管家点 → 计费 + 本卡改灰。保洁不在此群 = 点不到 = 天然防自批。
    不同意就不点（无「驳回」按钮，留白即拒）。
    """
    md = (
        f"**房间**\n{room_name}\n\n"
        f"**客人**\n{_md_safe(guest_name)}\n\n"
        f"保洁已申请打扫此房。核对无误点【通过】；不通过就不用点。"
    )
    common = {"room_id": room_id, "order_id": order_id, "request_id": request_id}
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"🧾 打扫申请待审核 · {room_name}"},
        },
        "body": {"elements": [
            {"tag": "markdown", "content": md},
            {"tag": "hr"},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "通过"},
                "type": "primary",
                "behaviors": [{"type": "callback", "value": {"action": "cr_approve", **common}}],
            },
        ]},
    }


async def send_cleaning_review_card(
    *, room_name: str, guest_name: str, request_id: str, room_id: str, order_id: str,
) -> str | None:
    """发审核卡到审核保洁群。返回 message_id / None。仅应用模式（回传按钮）有意义。"""
    if not _cleaning_request_app_configured():
        return None
    card = _build_cleaning_review_card(
        room_name=room_name, guest_name=guest_name,
        request_id=request_id, room_id=room_id, order_id=order_id,
    )
    return await send_card_via_app(chat_id=_cleaning_review_chat_id(), card=card)


def _build_cleaning_request_grey_card(*, title: str, line: str) -> dict:
    """打扫申请流程的通用灰卡（密码卡扫完 / 审核卡通过 后原地改灰）。schema 2.0。"""
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": [{"tag": "markdown", "content": line}]},
    }


async def grey_cleaning_password_card(
    *, message_id: str, room_name: str, trial_tag: str | None = None,
) -> None:
    """扫完后把私聊密码卡改灰（密码失效）。best-effort。"""
    if not message_id:
        return
    await _patch_message_card(
        message_id,
        _build_cleaning_request_grey_card(
            title=_title_with_trial(f"✅ 已打扫 · {room_name}", trial_tag),
            line="已记录打扫完成，门锁密码已失效。",
        ),
    )


async def grey_cleaning_review_card(*, message_id: str, room_name: str) -> None:
    """通过后把审核卡改灰（已计费）。best-effort。"""
    if not message_id:
        return
    await _patch_message_card(
        message_id,
        _build_cleaning_request_grey_card(
            title=f"✅ 已通过 · {room_name}", line="已通过。"
        ),
    )


async def send_cleaning_renewal_alert(
    *,
    room_name: str,
    guest_name: str,
    stay_range: str,
    notes: str | None = None,
) -> None:
    """续住/在住房打扫登记 → 给保洁群发一张告知卡（客人要求打扫）。

    与退房卡的关键区别：**纯告知，不带「打扫完了」回写按钮**——续住房里还住着人，
    回写会触发房态联动误锁房（评审红线）。故只提醒保洁去打扫，完成无需回写系统。
    未配置保洁群（webhook/应用皆无）时优雅跳过（_deliver 内建降级）。
    """
    fields = [
        {"is_short": False, "text": {"tag": "lark_md", "content": f"**房间**\n{room_name}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**客人**\n{guest_name}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**在住**\n{stay_range}"}},
    ]
    if notes:
        fields.append({"is_short": False, "text": {"tag": "lark_md",
                       "content": f"**备注**\n{notes}"}})
    card = _build_card(title="🧹 续住房打扫（客人要求）", template="blue", fields=fields)

    if _app_mode_configured():
        await send_card_via_app(chat_id=settings.FEISHU_CLEANING_CHAT_ID, card=card)
        return
    await _post_card_to_feishu(
        card,
        url=settings.FEISHU_CLEANING_WEBHOOK_URL,
        secret=settings.FEISHU_CLEANING_WEBHOOK_SECRET,
    )
