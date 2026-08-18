"""业主服务费费率 — 读取/写入配置的单一入口。

费率来源优先级：service_fee_config 单行表 → 缺行时回退 cleaning_pricing 默认常量。
退房记账（cleaning.add_checkout_cleaning_charge）与财务设置 API 都只经此模块，
不再直接读 cleaning_pricing 常量，保证「财务页改费率即时生效」。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cleaning_pricing import (
    CHECKOUT_CLEANING_FEE,
    INSTAY_CLEANING_FEE,
    LAUNDRY_FEE_PER_ROOM,
    CONSUMABLE_FEE_PER_ROOM_NIGHT,
)
from app.models.service_fee_config import ServiceFeeConfig, SERVICE_FEE_CONFIG_ID


@dataclass(frozen=True)
class ServiceFees:
    """一次快照的四个费率（Decimal）。记账时读一次，避免多次查库。"""
    checkout_cleaning_fee: Decimal
    instay_cleaning_fee: Decimal
    laundry_fee_per_room: Decimal
    consumable_fee_per_room_night: Decimal


def _defaults() -> ServiceFees:
    return ServiceFees(
        checkout_cleaning_fee=CHECKOUT_CLEANING_FEE,
        instay_cleaning_fee=INSTAY_CLEANING_FEE,
        laundry_fee_per_room=LAUNDRY_FEE_PER_ROOM,
        consumable_fee_per_room_night=CONSUMABLE_FEE_PER_ROOM_NIGHT,
    )


async def get_service_fees(db: AsyncSession) -> ServiceFees:
    """读当前费率快照。配置行缺失（如新库未 seed / 测试）时回退默认常量。"""
    row = (
        await db.execute(
            select(ServiceFeeConfig).where(
                ServiceFeeConfig.config_id == SERVICE_FEE_CONFIG_ID
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return _defaults()
    return ServiceFees(
        checkout_cleaning_fee=Decimal(str(row.checkout_cleaning_fee)),
        instay_cleaning_fee=Decimal(str(row.instay_cleaning_fee)),
        laundry_fee_per_room=Decimal(str(row.laundry_fee_per_room)),
        consumable_fee_per_room_night=Decimal(str(row.consumable_fee_per_room_night)),
    )


async def get_or_create_config_row(db: AsyncSession) -> ServiceFeeConfig:
    """取配置行，缺则按默认常量建一行（供设置 API 读/改）。不 commit，调用方负责。"""
    row = (
        await db.execute(
            select(ServiceFeeConfig).where(
                ServiceFeeConfig.config_id == SERVICE_FEE_CONFIG_ID
            )
        )
    ).scalar_one_or_none()
    if row is None:
        d = _defaults()
        row = ServiceFeeConfig(
            config_id=SERVICE_FEE_CONFIG_ID,
            checkout_cleaning_fee=d.checkout_cleaning_fee,
            instay_cleaning_fee=d.instay_cleaning_fee,
            laundry_fee_per_room=d.laundry_fee_per_room,
            consumable_fee_per_room_night=d.consumable_fee_per_room_night,
        )
        db.add(row)
        await db.flush()
    return row
