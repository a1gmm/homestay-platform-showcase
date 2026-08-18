from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from typing import Optional
import uuid

from app.core.deps import DBSession, CurrentUser
from app.core.security import hash_password
from app.models.owner import Owner
from app.models.room import Room
from app.schemas.owner import (
    OwnerCreate, OwnerUpdate, OwnerOut, OwnerRoomItem, BatchAssignOwnerBody, SubOwnerLite,
)
from app.services.audit import log_action_tx
from app.services.owner_scope import get_sub_owners

router = APIRouter(prefix="/owners", tags=["owners"])


def _to_out(owner: Owner, rooms: list[Room], sub_owners: list[Owner] | None = None) -> OwnerOut:
    subs = sub_owners or []
    return OwnerOut(
        owner_id=owner.owner_id,
        name=owner.name,
        username=owner.username,
        phone=owner.phone,
        id_card=owner.id_card,
        bank_account=owner.bank_account,
        bank_name=owner.bank_name,
        notes=owner.notes,
        created_at=owner.created_at,
        parent_owner_id=owner.parent_owner_id,
        is_master=len(subs) > 0,
        sub_owners=[SubOwnerLite(owner_id=s.owner_id, name=s.name) for s in subs],
        rooms=[
            OwnerRoomItem(
                room_id=r.room_id,
                room_name=r.room_name,
                owner_share_ratio=r.owner_share_ratio,
                owner_deduction_rules=list(r.owner_deduction_rules or []),
                owner_ignored_categories=list(getattr(r, "owner_ignored_categories", None) or []),
            )
            for r in rooms
        ],
        room_count=len(rooms),
    )


@router.get("", response_model=list[OwnerOut])
async def list_owners(db: DBSession, current_user: CurrentUser):
    if current_user["role"] not in ("admin", "finance", "operator"):
        raise HTTPException(status_code=403, detail="无权查看业主")

    result = await db.execute(
        select(Owner).options(selectinload(Owner.rooms)).order_by(Owner.name)
    )
    owners = result.scalars().all()
    # 标记总账号：从已加载的业主列表按 parent_owner_id 反推每个账号的子业主，
    # 零额外查询。_to_out 据此把 is_master / sub_owners 填上，列表才能显示「总账号」标签。
    children_by_parent: dict[str, list[Owner]] = {}
    for o in owners:
        if o.parent_owner_id:
            children_by_parent.setdefault(o.parent_owner_id, []).append(o)
    return [
        _to_out(o, list(o.rooms or []), children_by_parent.get(o.owner_id))
        for o in owners
    ]


@router.get("/{owner_id}", response_model=OwnerOut)
async def get_owner(owner_id: str, db: DBSession, current_user: CurrentUser):
    if current_user["role"] not in ("admin", "finance", "operator", "owner"):
        raise HTTPException(status_code=403, detail="无权查看业主")
    result = await db.execute(
        select(Owner).options(selectinload(Owner.rooms)).where(Owner.owner_id == owner_id)
    )
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="业主不存在")
    subs = await get_sub_owners(db, owner_id)
    return _to_out(owner, list(owner.rooms or []), subs)


@router.post("", response_model=OwnerOut, status_code=201)
async def create_owner(body: OwnerCreate, db: DBSession, current_user: CurrentUser):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可新建业主")

    owner_id = body.owner_id or ("OWN-" + uuid.uuid4().hex[:10].upper())
    # 唯一性校验
    exists = await db.execute(select(Owner).where(Owner.owner_id == owner_id))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="业主编号已存在")
    if body.username:
        dup = await db.execute(
            select(Owner.owner_id).where(Owner.username == body.username)
        )
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="该账号已被占用")

    owner = Owner(
        owner_id=owner_id,
        name=body.name,
        username=body.username,
        phone=body.phone,
        id_card=body.id_card,
        bank_account=body.bank_account,
        bank_name=body.bank_name,
        notes=body.notes,
    )
    db.add(owner)
    await log_action_tx(db, current_user["user_id"], "owner.create", "owner", owner.owner_id)
    await db.commit()
    await db.refresh(owner)
    return _to_out(owner, [])


@router.patch("/{owner_id}", response_model=OwnerOut)
async def update_owner(
    owner_id: str, body: OwnerUpdate, db: DBSession, current_user: CurrentUser
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改业主")
    result = await db.execute(
        select(Owner).options(selectinload(Owner.rooms)).where(Owner.owner_id == owner_id)
    )
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="业主不存在")

    payload = body.model_dump(exclude_unset=True)
    if "username" in payload and payload["username"] is not None and payload["username"] != owner.username:
        dup = await db.execute(
            select(Owner.owner_id).where(
                Owner.username == payload["username"], Owner.owner_id != owner_id
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="该账号已被占用")

    if "parent_owner_id" in payload:
        new_parent = payload["parent_owner_id"]
        if new_parent:
            if new_parent == owner_id:
                raise HTTPException(status_code=400, detail="不能把自己设为上级账号")
            parent = (await db.execute(
                select(Owner).options(selectinload(Owner.rooms))
                .where(Owner.owner_id == new_parent)
            )).scalar_one_or_none()
            if not parent:
                raise HTTPException(status_code=404, detail="上级账号不存在")
            if parent.parent_owner_id:
                raise HTTPException(status_code=400, detail="上级账号本身已是子账号，不支持多级嵌套")
            if parent.rooms:
                raise HTTPException(status_code=400, detail="上级账号名下有房间，不能作为总账号")
            has_children = (await db.execute(
                select(Owner.owner_id).where(Owner.parent_owner_id == owner_id).limit(1)
            )).scalar_one_or_none()
            if has_children:
                raise HTTPException(status_code=400, detail="该业主本身是总账号，不能再挂到别的账号下")

    for field, val in payload.items():
        setattr(owner, field, val)
    await log_action_tx(db, current_user["user_id"], "owner.update", "owner", owner_id)
    await db.commit()
    await db.refresh(owner)
    subs = await get_sub_owners(db, owner_id)
    return _to_out(owner, list(owner.rooms or []), subs)


@router.delete("/{owner_id}", status_code=204)
async def delete_owner(owner_id: str, db: DBSession, current_user: CurrentUser):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可删除业主")

    # 检查名下是否还有房间
    rooms_count = await db.execute(
        select(Room).where(Room.owner_id == owner_id).limit(1)
    )
    if rooms_count.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="该业主名下仍有房间，请先解除房间关联",
        )

    result = await db.execute(select(Owner).where(Owner.owner_id == owner_id))
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="业主不存在")
    await db.delete(owner)
    await log_action_tx(db, current_user["user_id"], "owner.delete", "owner", owner_id)
    await db.commit()


class OwnerResetPasswordBody(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("密码长度不能少于8位")
        if v.isdigit() or v.isalpha():
            raise ValueError("密码必须包含字母和数字")
        return v


@router.post("/{owner_id}/reset-password")
async def reset_owner_password(
    owner_id: str,
    body: OwnerResetPasswordBody,
    db: DBSession,
    current_user: CurrentUser,
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可重置业主密码")

    result = await db.execute(select(Owner).where(Owner.owner_id == owner_id))
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="业主不存在")

    owner.password_hash = hash_password(body.new_password)
    await log_action_tx(
        db,
        operator_id=current_user["user_id"],
        action="owner.reset_password",
        resource_type="owner",
        resource_id=owner_id,
        notes="管理员重置业主密码",
    )
    await db.commit()
    return {"message": "密码已重置"}


@router.post("/batch-assign-rooms")
async def batch_assign_rooms(
    body: BatchAssignOwnerBody, db: DBSession, current_user: CurrentUser
):
    """
    批量把房间关联到业主，并可选设置分成比例和扣除规则。
    owner_id=None 表示解除关联。
    """
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可分配业主")
    if not body.room_ids:
        raise HTTPException(status_code=400, detail="请选择至少一间房")

    if body.owner_id:
        owner_exists = await db.execute(select(Owner).where(Owner.owner_id == body.owner_id))
        if not owner_exists.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="业主不存在")
        has_children = (await db.execute(
            select(Owner.owner_id).where(Owner.parent_owner_id == body.owner_id).limit(1)
        )).scalar_one_or_none()
        if has_children:
            raise HTTPException(status_code=400, detail="该账号是总账号，不能直接持有房间")

    result = await db.execute(select(Room).where(Room.room_id.in_(body.room_ids)))
    rooms = result.scalars().all()
    if len(rooms) != len(body.room_ids):
        missing = set(body.room_ids) - {r.room_id for r in rooms}
        raise HTTPException(status_code=404, detail=f"房间不存在: {','.join(missing)}")

    for room in rooms:
        room.owner_id = body.owner_id
        if body.owner_share_ratio is not None:
            room.owner_share_ratio = body.owner_share_ratio
        if body.owner_deduction_rules is not None:
            room.owner_deduction_rules = body.owner_deduction_rules
        if body.owner_ignored_categories is not None:
            room.owner_ignored_categories = body.owner_ignored_categories

    await log_action_tx(
        db, current_user["user_id"], "owner.batch_assign", "owner",
        body.owner_id or "unassigned",
        after_data={"room_ids": body.room_ids, "ratio": str(body.owner_share_ratio)},
    )
    await db.commit()
    return {"updated": len(rooms)}
