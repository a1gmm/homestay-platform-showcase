"""Display-only OTA free-room classification."""

from collections.abc import Iterable


def is_free_room_type(room_type: object) -> bool:
    return isinstance(room_type, str) and room_type.strip().startswith("【免房】")


def free_room_kind(orders: Iterable[object]) -> str:
    values = [bool(getattr(order, "is_ota_free_room", False)) for order in orders]
    if not values or not any(values):
        return "none"
    return "all" if all(values) else "mixed"
