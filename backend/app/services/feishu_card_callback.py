"""飞书卡片「回传交互」回调的纯逻辑（保洁「打扫完了」一键回写）。

与 services/feishu_lead_alert.py 的「群机器人 webhook 单向推送」不同：回传交互
按钮要求卡片由**自建应用**发送，用户点击后飞书会回调到本服务的 endpoint。本模块
只放**纯函数**（解析回调 body / 校验 token / 构造应答），不含 I/O，便于单测。

约定（依据飞书开放平台「卡片回传交互回调」文档）：
- 回调 body：`header.token`=应用 Verification Token、`header.event_type`=
  "card.action.trigger"（新版卡片）或 "card.action.trigger_v1"（旧版 1.0 结构
  卡片，我们 `_build_card` 发的就是这种）、`event.action.value`=我们在按钮里塞的
  开发者自定义数据。点击人 open_id 新版在 `event.operator.open_id`，v1 事件体
  可能直挂 `event.open_id`；v1 的 value 可能是 JSON 字符串而非 dict。
- 配置回调 URL 时飞书先发 `{"type":"url_verification","challenge":...,"token":...}`，
  需原样回 `{"challenge": ...}`。
- 校验靠 `header.token == Verification Token`（前提：飞书后台**不开加密 Encrypt
  Key**，body 才是明文）。HTTPS 下足够。
"""
import hmac
import json

# 新旧两代卡片的点击回调事件名。只认新版漏掉 _v1 曾致「打扫完了」点击被当
# unknown 打发、回写不执行（2026-07-08 生产 200341 事故）。
_CARD_ACTION_EVENTS = ("card.action.trigger", "card.action.trigger_v1")


# 公开端点上 value 是任意可控字符串：超过此长度不进 json.loads（按钮 value 实际
# 只有 action/room_id/task_id 三个短字段，正常远小于此）。
_VALUE_MAX_LEN = 10_000


def _as_dict(v) -> dict:
    """攻击者可把 body 任意层换成 list/str/数字——归一成 dict，杜绝 AttributeError。"""
    return v if isinstance(v, dict) else {}


def _normalize_value(value) -> dict:
    """按钮 value 归一成 dict：容忍 JSON 字符串（限长防炸栈）与任意非 dict 类型。"""
    if isinstance(value, str):
        if len(value) > _VALUE_MAX_LEN:
            return {}
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):  # 坏 JSON / 深嵌套炸栈
            return {}
    return _as_dict(value)


def parse_card_callback(body) -> dict:
    """把飞书回调 body 归一成调用方好处理的 intent（总函数：任何输入都不抛异常）。

    - url_verification（配置回调地址时）→ {"kind":"challenge","challenge":<str>}
    - 点击回传按钮（card.action.trigger / trigger_v1）→
      {"kind":"action","open_id":<str>,"value":<dict>,"legacy":False}
    - 老管道「消息卡片请求网址」扁平报文（无 header，token/open_id/action 顶层）→
      {"kind":"action",...,"legacy":True}——调用方须用老管道应答格式（空对象）
    - 其它 → {"kind":"unknown"}
    """
    body = _as_dict(body)
    if body.get("type") == "url_verification":
        return {"kind": "challenge", "challenge": body.get("challenge", "")}

    header = _as_dict(body.get("header"))
    if header.get("event_type") in _CARD_ACTION_EVENTS:
        event = _as_dict(body.get("event"))
        operator = _as_dict(event.get("operator"))
        action = _as_dict(event.get("action"))
        # 被点卡片的 message_id（退押金一键改灰要用：点哪张灰哪张）。飞书把它放在
        # event.context.open_message_id；缺失/非串则归一为空串。
        context = _as_dict(event.get("context"))
        message_id = context.get("open_message_id")
        return {
            "kind": "action",
            "open_id": operator.get("open_id") or event.get("open_id", ""),
            "value": _normalize_value(action.get("value") or {}),
            "message_id": message_id if isinstance(message_id, str) else "",
            "legacy": False,
        }

    if not header and isinstance(body.get("action"), dict):
        open_id = body.get("open_id")
        return {
            "kind": "action",
            "open_id": open_id if isinstance(open_id, str) else "",
            "value": _normalize_value(_as_dict(body.get("action")).get("value") or {}),
            "message_id": "",  # 老管道扁平报文无 context
            "legacy": True,
        }

    return {"kind": "unknown"}


def verify_callback_token(body, expected_token: str) -> bool:
    """校验回调真实性：header.token == 应用 Verification Token（常量时间比较）。

    expected_token 为空（未配置）时一律拒绝——绝不放行未校验的回调。
    token 非字符串（攻击者可发数字/数组）一律拒绝；按 UTF-8 编码后比较，
    避免 compare_digest 对非 ASCII str 抛 TypeError。
    新管道 token 在 header.token；老管道扁平报文在顶层 token——两处都认。
    """
    if not expected_token:
        return False
    d = _as_dict(body)
    got = _as_dict(d.get("header")).get("token") or d.get("token")
    if not isinstance(got, str) or not got:
        return False
    return hmac.compare_digest(got.encode("utf-8"), expected_token.encode("utf-8"))


def build_toast_response(content: str, *, toast_type: str = "info") -> dict:
    """飞书要求 3 秒内应答：弹一个 toast 告诉点击人结果。"""
    return {"toast": {"type": toast_type, "content": content}}
