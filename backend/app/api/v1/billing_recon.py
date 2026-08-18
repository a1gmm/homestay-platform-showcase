# backend/app/api/v1/billing_recon.py
"""账单对账（billing-recon）端点。全部 admin-only。

AI 只在 upload 里做列映射（app/services/billing_recon/ai_mapping.py，
model=DeepSeek deepseek-chat）；对账/写库全是确定性代码（services/billing_recon/）。
"""
import logging
from datetime import datetime, timezone

import openai
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_role
from app.models.recon import ReconBatch, ReconDiff
from app.services.billing_recon.ai_mapping import AiMappingError
from app.services.billing_recon.engine import BillRejected, apply_diff_action, claim_match, run_recon
from app.services.billing_recon.parser import BillParseError
from app.services.billing_recon.upload import get_or_create_processing_batch, upload_fingerprint, validate_bill_container
from app.services.billing_recon.summary import build_live_summary, review_batch
from app.services.audit import log_action_tx

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing-recon", tags=["billing-recon"])

_MAX_UPLOAD = 10 * 1024 * 1024


def _batch_out(b: ReconBatch) -> dict:
    return {
        "batch_id": b.batch_id, "platform": b.platform, "bill_month": b.bill_month,
        "summary_total": str(b.summary_total), "row_count": b.row_count,
        "status": b.status, "error": b.error,
        "stats": (b.mapping or {}).get("stats", {}),
        "summary": (b.mapping or {}).get("summary", {}),
        "diagnosis": (b.mapping or {}).get("ai_diagnosis", {}),
        "ai_status": (b.mapping or {}).get("ai_status", "failed"),
        "reviewed_at": (b.mapping or {}).get("reviewed_at"),
        "reviewed_by": (b.mapping or {}).get("reviewed_by"),
        "filename": (b.mapping or {}).get("filename"),
        "archived_at": (b.mapping or {}).get("archived_at"),
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


def _diff_out(d: ReconDiff) -> dict:
    return {
        "diff_id": d.diff_id, "batch_id": d.batch_id, "order_id": d.order_id,
        "platform_order_id": d.platform_order_id, "guest_name": d.guest_name,
        "diff_class": d.diff_class.value, "status": d.status.value,
        "bill_amount": str(d.bill_amount) if d.bill_amount is not None else None,
        "system_amount": str(d.system_amount) if d.system_amount is not None else None,
        "detail": d.detail or {},
    }


async def _batch_diffs(db: AsyncSession, batch_id: str) -> list[ReconDiff]:
    rows = await db.execute(
        select(ReconDiff).where(ReconDiff.batch_id == batch_id).order_by(ReconDiff.diff_class, ReconDiff.diff_id)
    )
    return list(rows.scalars().all())


def _diff_class_counts(diffs: list[ReconDiff]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in diffs:
        key = d.diff_class.value
        counts[key] = counts.get(key, 0) + 1
    return counts


@router.post("/upload")
async def upload_bill(
    file: UploadFile = File(...),
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    if not settings.DEEPSEEK_API_KEY:
        raise HTTPException(503, "DEEPSEEK_API_KEY 未配置，账单对账不可用")
    # 先看 Content-Length 报的大小：读完再判等于已经把 500MB 拉进内存了，闸门形同虚设。
    # file.size 可能缺失/撒谎，读完后的实测检查照留。
    if file.size and file.size > _MAX_UPLOAD:
        raise HTTPException(413, "文件超过 10MB")
    data = await file.read()
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(413, "文件超过 10MB")

    try:
        validate_bill_container(data, file.filename or "")
        fingerprint = upload_fingerprint(data)
        processing_batch, created = await get_or_create_processing_batch(
            db,
            fingerprint=fingerprint,
            filename=file.filename or "",
            platform="ctrip",
            user_id=current["user_id"],
        )
        if not created:
            diffs = await _batch_diffs(db, processing_batch.batch_id)
            return {"batch": _batch_out(processing_batch), "diffs": [_diff_out(d) for d in diffs]}
        batch = await run_recon(db, data=data, filename=file.filename or "",
                                platform="ctrip", user_id=current["user_id"],
                                upload_fingerprint=fingerprint, processing_batch=processing_batch)
    except BillParseError:
        raise HTTPException(422, "无法解析该文件：请上传携程账单原始 xls/xlsx")
    except BillRejected as e:
        raise HTTPException(422, detail={"errors": e.errors, "batch_id": e.batch.batch_id})
    except (openai.APIStatusError, openai.APIConnectionError):
        # APITimeoutError 是 APIConnectionError 子类，勿单列，否则更具体的分支永远轮不到。
        raise HTTPException(503, "AI 服务暂时不可用，请稍后重试")
    except AiMappingError as e:
        # 只认 AI 认列这一步自己的失败；别再宽泛地吞 RuntimeError——
        # 深处冒上来的 bug 该以 500 暴露，不该伪装成"AI 服务暂时不可用"让人去重试。
        raise HTTPException(503, str(e))

    diffs = await _batch_diffs(db, batch.batch_id)
    try:
        from app.services.feishu_lead_alert import send_billing_recon_alert

        counts = _diff_class_counts(diffs)
        counts_text = "、".join(f"{k}×{v}" for k, v in counts.items()) or "无"
        await send_billing_recon_alert(
            f"📒 账单对账 {batch.bill_month}：账单合计 ¥{batch.summary_total}，"
            f"共 {batch.row_count} 行，发现差异 {len(diffs)} 条（{counts_text}）。"
            f"批次 {batch.batch_id}，点击直接处理："
            f"{settings.FRONTEND_BASE_URL}/finance/billing-recon?batch={batch.batch_id}"
        )
    except Exception:  # noqa: BLE001 — 摘要失败不影响主流程
        logger.warning("billing-recon feishu summary failed", exc_info=True)

    return {"batch": _batch_out(batch), "diffs": [_diff_out(d) for d in diffs]}


@router.get("/batches")
async def list_batches(current=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(ReconBatch).order_by(ReconBatch.created_at.desc()).limit(100))
    visible = [b for b in rows.scalars().all() if not (b.mapping or {}).get("archived_at")]
    return [_batch_out(b) for b in visible[:50]]


@router.post("/batches/{batch_id}/archive")
async def archive_batch(
    batch_id: str,
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    batch = (await db.execute(
        select(ReconBatch).where(ReconBatch.batch_id == batch_id).with_for_update()
    )).scalar_one_or_none()
    if batch is None:
        raise HTTPException(404, "批次不存在")
    mapping = dict(batch.mapping or {})
    if mapping.get("archived_at"):
        return {"archived_at": mapping["archived_at"]}
    archived_at = datetime.now(timezone.utc).isoformat()
    batch.mapping = {**mapping, "archived_at": archived_at, "archived_by": current["user_id"]}
    await log_action_tx(
        db, current["user_id"], "billing_recon.archive", "recon_batch", batch_id,
        before_data={"archived_at": None}, after_data={"archived_at": archived_at},
    )
    await db.commit()
    return {"archived_at": archived_at}


@router.get("/batches/{batch_id}")
async def batch_detail(batch_id: str, current=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    batch = await db.get(ReconBatch, batch_id)
    if batch is None or (batch.mapping or {}).get("archived_at"):
        raise HTTPException(404, "批次不存在")
    diffs = await _batch_diffs(db, batch_id)
    batch_out = _batch_out(batch)
    batch_out["summary"] = build_live_summary(diffs)
    return {"batch": batch_out, "diffs": [_diff_out(d) for d in diffs]}


@router.post("/batches/{batch_id}/review")
async def review_recon_batch(
    batch_id: str,
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await review_batch(db, batch_id, current["user_id"])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "批次不可用")
    return result


@router.post("/diffs/{diff_id}/action")
async def diff_action(
    diff_id: str,
    payload: dict,
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    diff = await db.get(ReconDiff, diff_id)
    if diff is None:
        raise HTTPException(404, "差异不存在")
    action = str(payload.get("action", ""))
    try:
        return await apply_diff_action(db, diff, action, current["user_id"])
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/diffs/{diff_id}/claim")
async def claim_diff(
    diff_id: str,
    payload: dict,
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    diff = await db.get(ReconDiff, diff_id)
    if diff is None:
        raise HTTPException(404, "差异不存在")
    order_id = str(payload.get("order_id", ""))
    if not order_id:
        raise HTTPException(400, "缺少 order_id")
    try:
        return await claim_match(db, diff, order_id, current["user_id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
