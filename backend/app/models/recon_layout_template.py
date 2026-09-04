"""Reusable, confirmed spreadsheet layouts for billing reconciliation."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReconLayoutTemplate(Base):
    __tablename__ = "recon_layout_templates"

    template_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    layout_signature: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    mapping: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    platform_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[str] = mapped_column(String(20), ForeignKey("users.user_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
