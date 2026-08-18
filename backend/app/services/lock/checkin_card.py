"""
入住卡片文案（观海居格式）。前台从飞书复制整条转发给客人。

★ 要改文案/电话/WiFi密码/楼道门密码等，改下面这几行常量即可。
自动按房间/订单填的只有：进门密码、Wi-Fi名、楼栋房号、入住退房日期。
"""
from __future__ import annotations

from datetime import date

# ── 固定内容（改这里）──────────────────────────────────────────────
PROPERTY_NAME = "观海居海景度假公寓"
COMMUNITY_NAME = "和昌海云曦岸"        # 小区名字
BUILDING = "1栋"                       # 楼栋（目前都在 1 栋）
CORRIDOR_CODE = "*#1415#"             # 楼道门密码（所有房统一）
WIFI_PREFIX = "guanhaiju"            # Wi-Fi 名 = 前缀 + 房号
WIFI_PASSWORD = "<YOUR_LOCAL_WIFI_PASSWORD>"           # Wi-Fi 密码（所有房统一）
CHECKOUT_TIME_TEXT = "12:00"         # 退房时间
COMPLAINT_PHONE = "13800138000"      # 投诉/客服电话
TIP = (
    "观海居海景度假公寓是一个注重卫生品质的公司，我们已在您入住前仔细检查卫生，"
    "为避免管家疏忽造成不好影响，入住时发现问题请第一时间与我们沟通，"
    "我们会第一时间为您解决处理。"
)


def build_checkin_card(
    *,
    room_name: str,
    door_code: str,
    check_in: date,
    check_out: date,
) -> str:
    """拼一张客人可直接收的入住卡片。"""
    return (
        f"🎊欢迎入住{PROPERTY_NAME}🎊\n\n"
        f"小区名字：{COMMUNITY_NAME}\n"
        f"楼栋房号：{BUILDING}-{room_name}\n"
        f"入住日期：{check_in} 至 {check_out}\n\n"
        f"楼道门密码：{CORRIDOR_CODE}\n"
        f"进门密码：{door_code}\n"
        f"ℹ️ 开锁：手触键盘亮起 → 输入进门密码 → 按 # 键；密码退房当天有效\n\n"
        f"Wi-Fi名称：{WIFI_PREFIX}{room_name}\n"
        f"Wi-Fi密码：{WIFI_PASSWORD}\n\n"
        f"🧳 退房：离店时在微信群里说一声「退房」即可，请于退房当天 {CHECKOUT_TIME_TEXT} 前离店，"
        f"带好随身物品、关好门即可（无需归还钥匙）。\n\n"
        f"温馨提示：\n{TIP}投诉☎：{COMPLAINT_PHONE}"
    )


def build_deposit_upload_card(*, room_label: str, deposit, upload_url: str) -> dict:
    """密码群里入住卡后紧跟的「上传押金小票」按钮卡（webhook interactive）。

    按钮=打开链接（免登录拍照页，令牌已绑本订单），不走回调，故 webhook 直接发即可。
    这张是给前台看的内部卡（不转发给客人）：收完押金点按钮拍小票存档，退房退押金时
    退押金卡上会直接显示这张图，扫屏退款。
    """
    # 押金金额前台经常不在系统里填（POS 机刷的），为 0/空时不显示误导性的「¥0」。
    deposit_line = f"押金：¥{deposit}\n" if deposit and deposit > 0 else ""
    body = (
        f"房间：{room_label}\n"
        f"{deposit_line}"
        "收了押金后点下方按钮拍照上传纸质小票，退房退押金时在退押金卡上直接调出扫码退款。"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "📸 上传押金小票"},
        },
        "elements": [
            {"tag": "div", "fields": [
                {"is_short": False, "text": {"tag": "lark_md", "content": body}},
            ]},
            {"tag": "hr"},
            {"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "上传押金小票"},
                "url": upload_url,
                "type": "primary",
            }]},
        ],
    }
