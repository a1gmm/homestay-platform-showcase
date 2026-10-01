"""Private recovery snapshots for administrator-deleted cleaning evidence."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CleaningWorkRemoval(Base):
    __tablename__ = "cleaning_work_removals"
    removal_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    import_id: Mapped[str] = mapped_column(
        ForeignKey("cleaning_work_imports.import_id"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("monthly_close_documents.document_id"), index=True, nullable=False
    )
    record_type: Mapped[str] = mapped_column(String(24), nullable=False)
    record_id: Mapped[str] = mapped_column(String(24), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    deleted_by: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restored_by: Mapped[str | None] = mapped_column(ForeignKey("users.user_id"))
