"""Persistence helpers for confirmed, reusable billing workbook layouts."""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recon_layout_template import ReconLayoutTemplate
from app.services.billing_recon.analysis import MappingCoordinates, PlatformScope


def coordinates_for_workbook(
    stored_mapping: dict,
    sheet_names: list[str],
) -> MappingCoordinates | None:
    """Resolve a confirmed layout against current sheet names without guessing.

    Monthly supplier exports commonly rename ``六月账单`` to ``七月账单``.
    New templates retain sheet positions; legacy single-sheet templates can be
    migrated safely because there is only one possible target.
    """
    if not sheet_names:
        return None
    mapping = dict(stored_mapping)
    sheet = mapping.get("sheet")
    if sheet not in sheet_names:
        sheet_index = mapping.get("_sheet_index")
        if isinstance(sheet_index, int) and 0 <= sheet_index < len(sheet_names):
            mapping["sheet"] = sheet_names[sheet_index]
        elif len(sheet_names) == 1:
            mapping["sheet"] = sheet_names[0]
        else:
            return None
    summary = mapping.get("summary_cell")
    if isinstance(summary, dict) and summary.get("sheet") not in sheet_names:
        summary_index = mapping.get("_summary_sheet_index")
        if isinstance(summary_index, int) and 0 <= summary_index < len(sheet_names):
            mapping["summary_cell"] = {
                **summary,
                "sheet": sheet_names[summary_index],
            }
        elif len(sheet_names) == 1:
            mapping["summary_cell"] = {**summary, "sheet": sheet_names[0]}
        else:
            return None
    mapping.pop("_sheet_index", None)
    mapping.pop("_summary_sheet_index", None)
    return MappingCoordinates.model_validate(mapping)


async def find_layout_template(
    db: AsyncSession, signature: str,
) -> ReconLayoutTemplate | None:
    """Find a layout without treating the suggestion as a confirmed use."""
    result = await db.execute(
        select(ReconLayoutTemplate).where(ReconLayoutTemplate.layout_signature == signature)
    )
    return result.scalar_one_or_none()


def _insert_for_layout_template(db: AsyncSession):
    """Build the native conflict-safe insert supported by production and tests."""
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"Unsupported layout-template dialect: {dialect_name}")
    return insert(ReconLayoutTemplate)


async def save_layout_template(
    db: AsyncSession,
    *,
    signature: str,
    coordinates: MappingCoordinates,
    platform_scope: PlatformScope,
    user_id: str,
    sheet_names: list[str] | None = None,
) -> ReconLayoutTemplate:
    """Create or refresh a layout after the operator has confirmed it."""
    now = datetime.now(timezone.utc)
    mapping = coordinates.model_dump(mode="json")
    if sheet_names is not None:
        try:
            mapping["_sheet_index"] = sheet_names.index(coordinates.sheet)
        except ValueError:
            pass
        if coordinates.summary_cell is not None:
            try:
                mapping["_summary_sheet_index"] = sheet_names.index(
                    coordinates.summary_cell.sheet
                )
            except ValueError:
                pass
    statement = _insert_for_layout_template(db).values(
        template_id=f"RLT-{uuid4().hex[:12].upper()}",
        layout_signature=signature,
        mapping=mapping,
        platform_scope=platform_scope.value,
        created_by=user_id,
        use_count=1,
        last_used_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[ReconLayoutTemplate.layout_signature],
        set_={
            "mapping": statement.excluded.mapping,
            "platform_scope": statement.excluded.platform_scope,
            "use_count": ReconLayoutTemplate.use_count + 1,
            "last_used_at": now,
            "updated_at": func.now(),
        },
    ).returning(ReconLayoutTemplate).execution_options(populate_existing=True)
    return (await db.execute(statement)).scalar_one()
