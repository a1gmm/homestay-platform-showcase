"""Administrator-only source workspace; all files remain evidence until confirmed."""
from typing import Annotated
from urllib.parse import quote
from fastapi import APIRouter, Depends, File, UploadFile, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from app.core.deps import DBSession, CurrentDBUser
from app.services.financial_case import service
from app.services.monthly_close.workflow import get_or_create_cycle, get_cycle_by_month, MonthlyCloseConflict
from app.services.monthly_close.permissions import assert_monthly_close_feature_enabled, MonthlyCloseFeature, MonthlyClosePermissionDenied

router = APIRouter(prefix='/monthly-close', tags=['monthly-close'])


async def admin(billing_month: str, user: CurrentDBUser):
    import re
    if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',billing_month):
        raise HTTPException(422,'月份必须是 YYYY-MM')
    try:
        assert_monthly_close_feature_enabled(MonthlyCloseFeature.natural_language, user)
    except MonthlyClosePermissionDenied as exc:
        raise HTTPException(403,str(exc)) from exc
    if getattr(user.role,'value',user.role) != 'admin':
        raise HTTPException(403,'此功能仅管理员可用')
    return user


Admin = Annotated[object, Depends(admin)]


@router.post('/{billing_month}/financial-case/files')
async def upload(billing_month: str, db: DBSession, user: Admin, file: UploadFile = File(...)):
    try:
        cycle=await get_or_create_cycle(db,billing_month,user.user_id)
        data=await file.read(10*1024*1024+1)
        result=await service.receive_file(db,cycle,user,data,file.filename or 'file',file.content_type)
        await db.commit()
        return result
    except (ValueError, MonthlyCloseConflict) as exc:
        await db.rollback()
        raise HTTPException(422,str(exc)) from exc


@router.get('/{billing_month}/financial-case/sources')
async def sources(billing_month: str, db: DBSession, user: Admin, offset: int = Query(0,ge=0), limit: int = Query(100,ge=1,le=200)):
    cycle=await get_cycle_by_month(db,billing_month)
    if not cycle: raise HTTPException(404,'月份不存在')
    entries=await service.load_sources(db,cycle.cycle_id)
    facts=[dict(source_id=s.source_id,filename=s.filename,**fact,decision=s.decisions.get(fact['key']))
           for s in entries for fact in s.parsed.get('facts',[])]
    return dict(sources=[service.receipt(s) for s in entries],total=len(facts),facts=facts[offset:offset+limit])


class Interpretation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    fact_key: str = Field(min_length=64,max_length=64)
    decision: dict


class InterpretationBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    changes: list[Interpretation] = Field(min_length=1,max_length=2000)


@router.post('/{billing_month}/financial-case/interpretations')
async def interpretations(billing_month: str, body: InterpretationBody, db: DBSession, user: Admin):
    cycle=await get_cycle_by_month(db,billing_month)
    if not cycle: raise HTTPException(404,'月份不存在')
    try:
        count=await service.apply_decisions(db,cycle,user,[c.model_dump() for c in body.changes])
        await db.commit()
        return dict(updated=count)
    except (ValueError,MonthlyCloseConflict) as exc:
        await db.rollback()
        raise HTTPException(422,str(exc)) from exc


@router.get('/{billing_month}/financial-case/export')
async def export(billing_month: str, db: DBSession, user: Admin, months: str | None = None):
    cycle=await get_cycle_by_month(db,billing_month)
    if not cycle: raise HTTPException(404,'月份不存在')
    sources=await service.load_sources(db,cycle.cycle_id)
    if not sources: raise HTTPException(404,'尚未上传资料')
    from app.services.financial_case.reporting import build_report, export_workbook
    report_months = months.split(',') if months else service._months('从开始累计',billing_month,sources)
    import re
    if len(report_months)>120 or any(not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',m) for m in report_months):
        raise HTTPException(422,'月份格式无效')
    import asyncio
    payloads = [service.source_payload(s) for s in sources]
    decisions = {k:v for s in sources for k,v in s.decisions.items()}
    def render():
        return export_workbook(build_report(payloads,decisions,report_months),payloads)
    data = await asyncio.to_thread(render)
    return Response(data,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition':f"attachment; filename*=UTF-8''{quote('对账报告-'+billing_month+'.xlsx')}",'Cache-Control':'no-store'})


@router.post('/{billing_month}/financial-case/review-checklist')
async def review_checklist(billing_month: str, db: DBSession, user: Admin):
    from app.services.financial_case.feedback import create_checklist
    try:
        cycle = await get_or_create_cycle(db,billing_month,user.user_id)
        data = await create_checklist(db,cycle,user)
        await db.commit()
    except (ValueError, MonthlyCloseConflict) as exc:
        await db.rollback()
        raise HTTPException(422,str(exc)) from exc
    return Response(data,media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(billing_month+'结算待核实清单.docx')})
