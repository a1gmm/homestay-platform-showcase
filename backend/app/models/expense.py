from sqlalchemy import String, Numeric, Text, Enum as PgEnum, DateTime, Date, ForeignKey, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import date, datetime
from decimal import Decimal
import enum

from app.core.database import Base


class ExpenseCategory(str, enum.Enum):
    # —— 保留兼容（v1 老数据仍能读）—— 不在 v2 业主费用占比 UI 中显示
    cleaning = "cleaning"               # 保洁（v2 仍使用）
    maintenance = "maintenance"         # 维修费（v2 仍使用）
    utilities = "utilities"             # @deprecated 旧的"水电费"统称
    supplies = "supplies"               # v2#7 复用为"采购费"（原"用品(旧)"，已启用）
    platform_fee = "platform_fee"       # 平台佣金，财务用
    tax = "tax"                         # 税，财务用
    other = "other"                     # 其他费用（v2 仍使用）
    # —— v2-issue#1 新增（按王总参考 PMS 截图顺序）——
    public_utilities = "public_utilities"           # 公摊水费
    cold_water = "cold_water"                       # @deprecated v2#7 合并进 water"水费"，只留历史读
    broadband = "broadband"                         # 宽带费
    daily_supplies = "daily_supplies"               # 日耗
    laundry = "laundry"                             # 洗涤
    hot_water = "hot_water"                         # @deprecated v2#7 合并进 water"水费"，只留历史读
    gas = "gas"                                     # 燃气费
    property_fee = "property_fee"                   # 物业费
    electricity = "electricity"                     # 电费
    kitchen_cleaning = "kitchen_cleaning"           # 厨房保洁
    property_guidance_fee = "property_guidance_fee" # 物业引导费
    # —— v2#7 对齐王总《业主分成明细》新增 ——
    water = "water"                                 # 水费（合并冷/热水）
    new_linen_prewash = "new_linen_prewash"         # 新布草过水费


# v2#7 业主费用支出占比配置 UI 显示这 15 个类目（对齐王总《业主分成明细》）。
# 改此列表须同步：frontend/lib/cost-share-constants.ts 三处 + seed 迁移
# 925d967c7c54 / <本次新 seed 迁移>。冷水/热水已合并进"水费(water)"停用。
OWNER_COST_SHARE_CATEGORIES: list[ExpenseCategory] = [
    ExpenseCategory.cleaning,
    ExpenseCategory.public_utilities,
    ExpenseCategory.water,
    ExpenseCategory.broadband,
    ExpenseCategory.daily_supplies,
    ExpenseCategory.laundry,
    ExpenseCategory.gas,
    ExpenseCategory.property_fee,
    ExpenseCategory.electricity,
    ExpenseCategory.maintenance,
    ExpenseCategory.other,
    ExpenseCategory.kitchen_cleaning,
    ExpenseCategory.property_guidance_fee,
    ExpenseCategory.new_linen_prewash,
    ExpenseCategory.supplies,
]

# 中文 label（前端可覆盖）
EXPENSE_CATEGORY_LABELS: dict[ExpenseCategory, str] = {
    ExpenseCategory.cleaning: "保洁",
    ExpenseCategory.maintenance: "维修费",
    ExpenseCategory.utilities: "水电费（旧）",
    ExpenseCategory.supplies: "采购费",
    ExpenseCategory.platform_fee: "平台佣金",
    ExpenseCategory.tax: "税费",
    ExpenseCategory.other: "其他费用",
    ExpenseCategory.public_utilities: "公摊水费",
    ExpenseCategory.cold_water: "冷水（旧）",
    ExpenseCategory.broadband: "宽带费",
    ExpenseCategory.daily_supplies: "日耗",
    ExpenseCategory.laundry: "洗涤",
    ExpenseCategory.hot_water: "热水（旧）",
    ExpenseCategory.gas: "燃气费",
    ExpenseCategory.property_fee: "物业费",
    ExpenseCategory.electricity: "电费",
    ExpenseCategory.kitchen_cleaning: "厨房保洁",
    ExpenseCategory.property_guidance_fee: "物业引导费",
    ExpenseCategory.water: "水费",
    ExpenseCategory.new_linen_prewash: "新布草过水费",
}


class ExpensePayer(str, enum.Enum):
    company = "company"
    owner = "owner"


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        Index("ix_expenses_date", "expense_date"),
        Index("ix_expenses_room_date", "room_id", "expense_date"),
    )

    expense_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    category: Mapped[ExpenseCategory] = mapped_column(PgEnum(ExpenseCategory, name="expense_category"))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    room_id: Mapped[str | None] = mapped_column(String(10), ForeignKey("rooms.room_id"))
    order_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("orders.order_id"))
    payer: Mapped[ExpensePayer] = mapped_column(PgEnum(ExpensePayer, name="expense_payer"), default=ExpensePayer.company)
    owner_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("owners.owner_id"))
    receipt_url: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # 系统自动生成的「业主服务费」标记（退房保洁/洗涤/日耗）。为 True 时业主结算强制
    # 100% 业主担，绕开 RoomCostShareRule 占比（这些是硬规则服务费，不受各房分摊配置影响）。
    # 手工录入的洗涤/日耗（is_service_fee=False）仍按占比规则走，行为不变。
    is_service_fee: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")
    # 软删除：误录支出只能作废、不能物理删（保留审计 + 不破坏历史分账溯源）。
    # is_deleted=True 从列表/by-room 汇总/看板/导出/业主结算 全部剔除（批2 item7）。
    is_deleted: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
