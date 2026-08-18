"""房间入住登记二维码 → 飞书 image_key 映射。

办理入住发完门锁密码卡片后，据此在密码群补发一张「该房间入住登记二维码」图片
（山东住宿登记，客人扫码自助登记）。二维码每房一张、基本不变，故 image_key 一次
上传后永久复用，静态存 checkin_qr_map.json（房号数字键，如 "1410"）。

生成/更新映射：把新图传飞书 im/v1/images 拿 image_key，改这个 JSON 即可
（键 = 纯房号数字，值 = image_key）。找不到 → 返回 None → 只发卡片不发图。
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_MAP_PATH = Path(__file__).with_name("checkin_qr_map.json")


@lru_cache(maxsize=1)
def _load_map() -> dict[str, str]:
    """读一次映射并缓存。文件缺失/损坏一律降级为空 dict（图片功能静默关闭，
    绝不影响门锁密码卡片本身）。"""
    try:
        data = json.loads(_MAP_PATH.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items() if v}
    except FileNotFoundError:
        logger.warning("checkin_qr_map.json 不存在，入住二维码图片功能关闭")
        return {}
    except Exception:
        logger.exception("checkin_qr_map.json 解析失败，入住二维码图片功能关闭")
        return {}


def _room_number(room_name: str) -> str:
    """从房名取纯数字键：'1410房' → '1410'，'A101' → '101'。"""
    return "".join(ch for ch in (room_name or "") if ch.isdigit())


def qr_image_key_for_room(room_name: str) -> str | None:
    """按房名查该房登记二维码的飞书 image_key；无则 None。"""
    return _load_map().get(_room_number(room_name)) or None
