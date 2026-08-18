from sqlalchemy import String, Text, DateTime, ForeignKey, Boolean, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship, backref
from datetime import datetime

from app.core.database import Base


class Owner(Base):
    __tablename__ = "owners"

    owner_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    username: Mapped[str | None] = mapped_column(String(50), unique=True)
    phone: Mapped[str | None] = mapped_column(String(20))
    id_card: Mapped[str | None] = mapped_column(String(20))
    bank_account: Mapped[str | None] = mapped_column(String(50))
    bank_name: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # 总账号支持：指向上级「总账号」的 owner_id。None = 普通/独立业主或总账号本身。
    # 总账号 = 本身无房间、但有子业主（parent_owner_id 指向它）的 owner。单层嵌套。
    parent_owner_id: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("owners.owner_id", ondelete="SET NULL"), nullable=True
    )
    # 「镜像视图」账号：登录后按 view_as_owner_id 指向的业主取数（看那位业主的真房间/入住/
    # 日历），而不是自己的房间。None = 普通账号看自己。用于演示号（如 zhanshi 看某真业主）。
    view_as_owner_id: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("owners.owner_id"), nullable=True
    )
    # 隐藏金额：True 时业主端所有接口把金额字段后端置 None（真数字不出后端）。
    # 与 view_as 搭配 = 给别人看的脱敏演示号；单独用也可让某账号看不到钱。
    hide_amounts: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # 隐藏客人信息：True 时业主端订单里的客人姓名后端脱敏为「客人」（演示号不露真实客人）。
    hide_guests: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    rooms = relationship("Room", back_populates="owner")
    settlements = relationship("OwnerSettlement", back_populates="owner")
    # 自引用邻接表：sub_owners = 子业主；parent = 上级总账号
    sub_owners = relationship(
        "Owner",
        backref=backref("parent", remote_side=[owner_id]),
        foreign_keys=[parent_owner_id],
    )
