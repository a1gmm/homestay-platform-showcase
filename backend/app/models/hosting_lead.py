from sqlalchemy import String, Text, DateTime, func, Index
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
from typing import Optional

from app.core.database import Base


class HostingLead(Base):
    """业主托管合作留资线索（来自 showcase.example.invalid 落地页）。"""

    __tablename__ = "hosting_leads"
    __table_args__ = (
        Index("ix_hosting_leads_phone_created", "phone", "created_at"),
        # flood 护栏的全局 count(created_at >= cutoff) 需要单列索引
        Index("ix_hosting_leads_created_at", "created_at"),
    )

    lead_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    property_location: Mapped[str] = mapped_column(String(200), nullable=False)
    source_ua: Mapped[Optional[str]] = mapped_column(Text)
    # Python 侧 default + server_default 双保险：SQLite 测试与 PG 生产行为一致，
    # 24h 去重查询在两种方言下可比较
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
