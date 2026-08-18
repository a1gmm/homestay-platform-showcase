"""业主服务费费率配置 — 单行单一来源（财务页可设置）。

保洁 65 / 续住 30 / 洗涤 15 / 日耗 22 的费率不再硬编码在 cleaning_pricing.py，
改由本表单行提供，财务页「费用标准设置」面板即时可改、无需发版。缺行时
读取方回退到 cleaning_pricing 里的默认常量（见 services/service_fees.py）。

金额均为「记入 Expense 的最终值」，房东全担 100%（王总 2026-07-12/07-14 拍板）。
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import String, Numeric, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 单行配置的固定主键。
SERVICE_FEE_CONFIG_ID = "default"


class ServiceFeeConfig(Base):
    __tablename__ = "service_fee_config"

    config_id: Mapped[str] = mapped_column(String(20), primary_key=True, default=SERVICE_FEE_CONFIG_ID)
    # 退房打扫（元/间/单）
    checkout_cleaning_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("65"))
    # 续住 / 日常打扫（元/间/次）
    instay_cleaning_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("30"))
    # 洗涤费（元/间/单，续住合并单只收一次）
    laundry_fee_per_room: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("15"))
    # 日耗品（元/间/夜）
    consumable_fee_per_room_night: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("22"))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(String(20))
