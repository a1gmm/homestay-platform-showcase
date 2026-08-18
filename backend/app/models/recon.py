"""账单对账（billing-recon）：批次 + 差异两表。

分类(diff_class)落库后不变；状态机按分类各自允许的终态（见设计文档）：
- fix_amount/compensation: pending → adopted / already_consistent / dismissed
- appeal: pending → appeal_pending → appeal_settled / dismissed
- broken_link: pending → acknowledged / dismissed
- manual_review: pending → dismissed（人工改判后走对应终态由后续批次重判）
重复上传同 (platform, bill_month)：旧批次仅 pending 作废；appeal_* 跨批次存活。
"""
import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import String, Numeric, Integer, Text, DateTime, ForeignKey, func, Index, Enum as PgEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReconDiffClass(str, enum.Enum):
    fix_amount = "fix_amount"          # 需修数：平台结算额 ≠ 系统到手
    appeal = "appeal"                  # 需申诉：系统有、账单无
    broken_link = "broken_link"        # 断链：靠客人名兜底匹配上，金额一致
    compensation = "compensation"      # 赔款：账单负向调整，系统无对应活跃单
    manual_review = "manual_review"    # 人工核对：同名多候选 / 完全匹配不上


class ReconDiffStatus(str, enum.Enum):
    pending = "pending"
    adopted = "adopted"
    already_consistent = "already_consistent"
    dismissed = "dismissed"
    appeal_pending = "appeal_pending"
    appeal_settled = "appeal_settled"
    acknowledged = "acknowledged"


class ReconBatch(Base):
    __tablename__ = "recon_batches"
    __table_args__ = (Index("ix_recon_batches_month", "platform", "bill_month"),)

    batch_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    platform: Mapped[str] = mapped_column(String(20), default="ctrip")
    bill_month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    summary_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="parsed")  # parsed | rejected
    error: Mapped[str | None] = mapped_column(Text)
    mapping: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReconDiff(Base):
    __tablename__ = "recon_diffs"
    __table_args__ = (
        Index("ix_recon_diffs_batch", "batch_id"),
        Index("ix_recon_diffs_status", "status"),
    )

    diff_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(40), ForeignKey("recon_batches.batch_id"))
    order_id: Mapped[str | None] = mapped_column(String(20))
    platform_order_id: Mapped[str | None] = mapped_column(String(100))
    guest_name: Mapped[str | None] = mapped_column(String(50))
    diff_class: Mapped[ReconDiffClass] = mapped_column(PgEnum(ReconDiffClass, name="recon_diff_class"))
    status: Mapped[ReconDiffStatus] = mapped_column(
        PgEnum(ReconDiffStatus, name="recon_diff_status"), default=ReconDiffStatus.pending
    )
    bill_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    system_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    resolved_by: Mapped[str | None] = mapped_column(String(20))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
