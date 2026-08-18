"""业主服务费默认单价 — 仅作 fallback 默认值（收房东）。

运行时费率的单一来源已改为 service_fee_config 单行表 + services/service_fees.py，
财务页可改、即时生效。本模块的常量只在配置行缺失（新库未 seed / 测试）时兜底。

- 退房打扫：65 元/间
- 续住 / 同订单 / 日常打扫：30 元/间
- 洗涤费：15 元/间/单（续住合并单只收一次）
- 日耗品：22 元/间/夜

金额从房东分成里扣（见 owner_settlement 结算），房东全担 100%（王总 2026-07-12/07-14 拍板），
故这里即为记入 Expense 的最终金额。保洁工资/保险由财务另算，本模块不涉及。
"""
from decimal import Decimal

CHECKOUT_CLEANING_FEE = Decimal("65")            # 退房打扫
INSTAY_CLEANING_FEE = Decimal("30")              # 续住 / 同订单 / 日常打扫
LAUNDRY_FEE_PER_ROOM = Decimal("15")             # 洗涤（每间每单一次）
CONSUMABLE_FEE_PER_ROOM_NIGHT = Decimal("22")    # 日耗品（每间每夜）
