"""Human-readable review sheets and bounded return-file ingestion.

Returned content is evidence only. No document paragraph or formula is executed.
Stable review keys map back to server-archived issues in the same workspace.
"""
from __future__ import annotations
import io
import re
import zipfile
from hashlib import sha256
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from app.services.billing_recon.parser import load_workbook_rows

W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
HEADERS=['事项编号','客人姓名','房号','入住日期','退房日期','当前问题与差异','需要核实什么','同事填写答案']


def docx_rows(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if len(z.infolist())>2000 or sum(i.file_size for i in z.infolist())>20*1024*1024:
                raise ValueError('Word 文件展开后过大，请拆分清单。')
            content=z.read('word/document.xml')
        if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():raise ValueError('不支持含外部实体的 Word 文档。')
        root=ET.fromstring(content)
    except (zipfile.BadZipFile,KeyError,ET.ParseError) as exc:
        raise ValueError('Word 文件损坏或格式不支持，请另存为 .docx 后重试。') from exc
    body=root.find(W+'body');rows=[]
    if body is None:return rows
    for child in body:
        if child.tag==W+'tbl':
            for tr in child.findall(W+'tr'):
                rows.append(['\n'.join(''.join(p.itertext()) for p in tc.findall(W+'p')) for tc in tr.findall(W+'tc')])
        elif child.tag==W+'p':
            value=''.join(child.itertext()).strip()
            if value:rows.append([value])
    if len(rows)>10000 or sum(len(str(x)) for r in rows for x in r)>500000:raise ValueError('核实清单内容过长，请分批处理。')
    return rows


def parse_feedback(data,filename):
    is_docx=filename.lower().endswith('.docx')
    if is_docx:sheets={'核实清单':docx_rows(data)}
    else:sheets,_=load_workbook_rows(data,filename)
    facts=[];has_header=False
    for sheet,rows in sheets.items():
        header=None; paired={}
        for n,row in enumerate(rows,1):
            texts=[str(c or '').strip() for c in row]
            if len(texts)==2 and texts[0]=='事项编号' and re.fullmatch(r'核实-[A-F0-9]{12}',texts[1]):
                paired={'事项编号':texts[1]};header=None;has_header=True;continue
            if paired and len(texts)==2:
                paired[texts[0]]=texts[1]
                if texts[0]=='同事填写答案':
                    facts.append(dict(key=sha256(f'{sha256(data).hexdigest()}:{sheet}:{n}'.encode()).hexdigest(),
                        kind='review_feedback',sheet=sheet,row1based=n,issue_id=paired['事项编号'],
                        answer=texts[1][:3000],description=paired.get('当前问题与差异','')[:1000],amount=None,business_month=None))
                    paired={}
                continue
            if '事项编号' in texts and '同事填写答案' in texts:
                header={v:i for i,v in enumerate(texts)};has_header=True;continue
            if header:
                def cell(name):
                    i=header.get(name);return texts[i] if i is not None and i<len(texts) else ''
                issue=cell('事项编号');answer=cell('同事填写答案')
                if not re.fullmatch(r'核实-[A-F0-9]{12}',issue):continue
                facts.append(dict(key=sha256(f'{sha256(data).hexdigest()}:{sheet}:{n}'.encode()).hexdigest(),
                    kind='review_feedback',sheet=sheet,row1based=n,issue_id=issue,answer=answer[:3000],
                    description=cell('当前问题与差异')[:1000],amount=None,business_month=None))
    if not is_docx and not has_header:return None
    if not has_header:
        # Older free-form return documents remain reviewable, without guessed associations.
        text='\n'.join(' | '.join(str(v or '') for v in r) for rows in sheets.values() for r in rows)
        facts=[dict(key=sha256(data).hexdigest(),kind='review_feedback_unmapped',sheet='核实清单',
                    row1based=1,answer=text[:20000],description='未带事项编号的回填内容',amount=None,business_month=None)]
    return dict(kind='feedback',facts=facts,summaries=[],templates=[],issues=[])


def issue_key(cycle_id,code,resource):
    return '核实-'+sha256(f'{cycle_id}:{code}:{resource}'.encode()).hexdigest()[:12].upper()


def source_answer(answer, month):
    """Only explicit accounting fields; never interpret document instructions as tools."""
    from .service import CATEGORIES
    if re.search(r'不是|不由|不要|假设|可能|待核实|不确定',answer):
        raise ValueError('请明确分类、所属月份和承担方，不能用否定或猜测作为账务依据。')
    result={}
    parties=[party for word,party in [('公司','company'),('业主','owner')] if word+'承担' in answer]
    if len(parties)>1:raise ValueError('同一条来源出现两个承担方，请明确分配依据。')
    if parties:result['payer']=parties[0]
    for word,party in [('公司','company'),('业主','owner')]:
        if word+'付款' in answer or word+'垫付' in answer:result['paid_by']=party
    categories=[key for key,label in CATEGORIES.items() if re.search(r'(?:分类|属于|记为|是)\s*[:：]?\s*'+re.escape(label)+r'(?:[，。；\s]|$)',answer)]
    if len(categories)>1:raise ValueError('分类不唯一，请明确这一条采用的费用类别。')
    if categories:result['category']=categories[0]
    period=re.search(r'(?:业务月份|所属月份|属于)\s*[:：]?\s*(20\d{2})[-年](0?[1-9]|1[0-2])月?',answer)
    if period:result['business_month']=f'{period[1]}-{int(period[2]):02}'
    elif re.search(r'属于本月|计入本月',answer):result['business_month']=month
    if re.search(r'不计入|排除本条',answer):result['include']=False
    if not result:raise ValueError('请写明分类、业务月份或承担方，例如“业主承担，公司付款，业务月份：2026-08”。')
    result['note']=answer[:500]
    return result


def render_docx(month,issues):
    def para(value, *, bold=False, color='292621', size=22):
        props=('<w:b/>' if bold else '')+f'<w:color w:val="{color}"/><w:sz w:val="{size}"/><w:rFonts w:eastAsia="宋体" w:ascii="Calibri"/>'
        return '<w:p><w:pPr><w:spacing w:after="100"/></w:pPr><w:r><w:rPr>'+props+'</w:rPr><w:t xml:space="preserve">'+escape(str(value or ''))+'</w:t></w:r></w:p>'
    body=para(month+' 结算待核实清单',bold=True,size=34)
    body+=para(f'共 {len(issues)} 项。每项单独填写；已经能自动处理的内容不需要同事重复核算。')
    body+=para('请填写“同事填写答案”。不确定的填“待核实”，并说明还缺什么依据。请保留事项编号，返回后系统会先展示方案，确认后才修改。',color='666666')
    if not issues:body+=para('当前检查未发现待核实事项。结算是否已确认、是否已打款，请分别查看系统状态。')
    for index,item in enumerate(issues,1):
        body+=para(f"{index}. {item.get('guest') or '待核实事项'}"+(' · '+item['room'] if item.get('room') else ''),bold=True,size=26)
        values=[('事项编号',item['issue_id']),('客人姓名',item.get('guest') or '不涉及客人／尚未对应'),
            ('房号',item.get('room') or '公共事项／尚未对应'),
            ('入住日期',item.get('check_in') or '—'),('退房日期',item.get('check_out') or '—'),
            ('当前问题与差异',item['message']),('需要核实什么',item.get('question') or '请填写正确数据、实际情况及依据。'),('同事填写答案','')]
        table='<w:tbl><w:tblPr><w:tblW w:w="9360" w:type="dxa"/><w:tblBorders>'+''.join('<w:'+edge+' w:val="single" w:sz="4" w:color="DDDDDD"/>' for edge in ['top','left','bottom','right','insideH','insideV'])+'</w:tblBorders></w:tblPr><w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="7360"/></w:tblGrid>'
        for label,value in values:
            table+='<w:tr><w:trPr><w:cantSplit/></w:trPr>'
            for width,content in [(2000,label),(7360,value)]:
                table+=f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>'+para(content,color='777777' if label=='事项编号' else '292621',size=18 if label=='事项编号' else 22)+'</w:tc>'
            table+='</w:tr>'
        body+=table+'</w:tbl>'+para('')
    document='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+body+'<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1000" w:right="1200" w:bottom="1000" w:left="1200"/></w:sectPr></w:body></w:document>'
    files={
        '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        '_rels/.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        'word/document.xml':document,
    }
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as target:
        for name,content in files.items():
            entry=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0));entry.compress_type=zipfile.ZIP_DEFLATED
            target.writestr(entry,content)
    return out.getvalue()


async def create_checklist(db,cycle,actor):
    from uuid import uuid4
    from app.models.financial_case import FinancialCaseSource
    from app.services.financial_case.service import require_admin,actor_id,load_sources
    from app.services.monthly_close.evidence import build_step_evidences
    from .operations import load_context,snapshot
    await require_admin(db,actor)
    sources=await load_sources(db,cycle.cycle_id)
    ctx=await load_context(db,cycle);by_id={o.order_id:o for o in ctx['orders']}
    reports=await build_step_evidences(db,cycle)
    issues=[]
    for i in [item for report in reports if report.step_key!='exception_clearance' for item in report.snapshot.get('issues',[])]:
        if i.get('origin')=='financial_case':
            source=next((s for s in sources if s.source_id==i.get('source_id')),None)
            key=issue_key(cycle.cycle_id,i['code'],str(i.get('fact_key') or i.get('source_id') or i['message'])+':'+str(source.version if source else 0))
            fact=next((f for f in source.parsed.get('facts',[]) if f.get('key')==i.get('fact_key')),None) if source else None
            evidence=(f"来源：{source.filename} · {fact.get('sheet','原表')} 第 {fact.get('row1based',fact.get('row','待核实'))} 行；金额 {fact.get('amount','待核实')} 元。" if source and fact else '')
            issues.append(dict(issue_id=key,key=key,kind='review_issue',code=i['code'],order_ids=[],
                source_id=i.get('source_id'),source_version=source.version if source else None,fact_key=i.get('fact_key'),message=i['message']+' '+evidence))
            continue
        order=by_id.get(i.get('order_id'));ids=[order.order_id] if order else [key for key in i.get('order_ids',[]) if key in by_id]
        specifics='；'.join(f'{label}：{i[key]}' for key,label in [('current_amount','当前金额'),('expected_amount','应计金额'),('amount','涉及金额'),('platform_order_id','平台订单号')] if i.get(key) is not None)
        bound_key=issue_key(cycle.cycle_id,i['code'],str(i['resource_id'])+':'+snapshot(ctx,ids))
        issues.append(dict(issue_id=bound_key,kind='review_issue',
            key=bound_key,code=i['code'],order_ids=ids,
            guest=order.guest_name if order else '',room='、'.join(ctx['room_map'][r.room_id].room_name if r.room_id in ctx['room_map'] else r.room_id for r in ctx['rooms'] if order and r.order_id==order.order_id),
            check_in=str(order.check_in_date) if order else '',check_out=str(order.check_out_date) if order else '',
            message=i['message']+('；'+specifics if specifics else ''),
            question=(i.get('action') or {}).get('label','请填写核实结果、正确数据及依据。'),snapshot=snapshot(ctx,ids)))
    issues=list({i['issue_id']:i for i in issues}.values())
    data=render_docx(cycle.billing_month,issues)
    fingerprint=sha256(data).hexdigest()
    from sqlalchemy import select
    existing=await db.scalar(select(FinancialCaseSource).where(FinancialCaseSource.cycle_id==cycle.cycle_id,FinancialCaseSource.sha256==fingerprint))
    if not existing:
        if len(sources)>=100:
            raise ValueError('当前月份已有 100 份来源，请先整理原件后再导出新清单。')
        db.add(FinancialCaseSource(source_id='FCS-'+uuid4().hex[:20].upper(),cycle_id=cycle.cycle_id,
            filename=cycle.billing_month+'结算待核实清单.docx',sha256=fingerprint,kind='checklist',content=data,
            parsed={'kind':'checklist','facts':issues,'issues':[]},decisions={},version=1,created_by=actor_id(actor)))
        from app.services.audit import log_action_tx
        await log_action_tx(db,actor_id(actor),'monthly_close.checklist_exported','monthly_close',cycle.cycle_id,
            after_data={'count':len(issues),'sha256':fingerprint})
    await db.flush()
    return data


async def review_return(db,cycle,actor,sources,attachment_ids,run_id,text):
    from uuid import uuid4
    from app.models.financial_case import FinancialCaseProposal
    from .operations import load_context,plan,snapshot,details,select_orders,digest,affected_months
    from .service import actor_id
    selected=[s for s in sources if s.source_id in attachment_ids and s.kind=='feedback']
    if not selected:return None
    known={f['issue_id']:f for s in sources if s.kind=='checklist' for f in s.parsed.get('facts',[])}
    ctx=await load_context(db,cycle);ops=[];pending=[];evidence=[];changes=[];source_details=[];bound_sources={s.source_id:s.version for s in selected}
    for source in selected:
        for fact in source.parsed['facts']:
            issue=known.get(fact.get('issue_id'));answer=fact.get('answer','').strip()
            if source.decisions.get(fact['key'],{}).get('status')=='applied':continue
            if not answer or answer in ('待核实','不清楚','不确定'):
                pending.append(dict(code='feedback_unanswered',message=(issue['message'] if issue else fact.get('description','回填事项'))+'；尚未填写明确结论。',source_id=source.source_id,fact_key=fact['key']))
                continue
            if not issue:
                pending.append(dict(code='feedback_target_unknown',message='回填内容没有对应本月原清单中的事项，请说明客人、房号和要修改的内容。',source_id=source.source_id,fact_key=fact['key']));continue
            if not issue.get('order_ids'):
                original=next((s for s in sources if s.source_id==issue.get('source_id')),None)
                try:
                    if not original or not issue.get('fact_key'):raise ValueError('此项需要补充原件或完成对应核对步骤，单独文字答复不能关闭。')
                    if original.version!=issue.get('source_version'):raise ValueError('原件解释已变化，请使用最新清单核实。')
                    change=dict(fact_key=issue['fact_key'],decision=source_answer(answer,cycle.billing_month))
                    from .service import validate_decisions
                    validate_decisions(sources,[change])
                    changes.append(change);bound_sources[original.source_id]=original.version
                    evidence.append(dict(source_id=source.source_id,fact_key=fact['key'],issue_id=issue['issue_id'],order_ids=[],changed_fact_key=issue['fact_key']))
                    source_details.append(dict(fact_key=issue['fact_key'],label=original.filename+' · 来源解释',value=answer[:500]+'；原始金额不变，保存解释后仍需生成记账方案。'))
                except ValueError as exc:
                    pending.append(dict(code='feedback_source_review',message=issue['message']+'；'+str(exc)+' 同事回复：'+answer[:200],source_id=source.source_id,fact_key=fact['key']))
                continue
            if snapshot(ctx,issue['order_ids'])!=issue['snapshot']:
                pending.append(dict(code='feedback_stale',message='回填期间该订单已变化，请对照最新记录重新核实。',source_id=source.source_id,fact_key=fact['key']));continue
            try:
                # Target comes from the archived checklist, never a user-edited ID cell.
                answer_ids={key.upper() for key in re.findall(r'ORD-[A-Za-z0-9-]+',answer,re.I)}
                if answer_ids - {key.upper() for key in issue['order_ids']}:
                    raise ValueError('回填答案包含原事项以外的订单，请分别核实，不能扩大原清单范围。')
                request=' '.join(issue['order_ids'])+' '+answer
                if issue['code']=='missing_platform_order_id':
                    value=re.fullmatch(r'(?:平台(?:订单号|单号)\s*(?:是|为|[:：])?\s*)?([A-Za-z0-9][A-Za-z0-9_-]{0,99})',answer)
                    if value:
                        planned=[dict(kind='correct_order',order_id=issue['order_ids'][0],channel=None,net=None,platform_id=value[1])]
                    else:planned=plan(ctx,cycle,request)
                else:planned=plan(ctx,cycle,request)
                planned_ids={op['order_id'] for op in planned}|{op['target_order_id'] for op in planned if op.get('target_order_id')}
                if planned_ids - set(issue['order_ids']):
                    raise ValueError('处理方案超出原事项绑定的订单范围，请重新核实。')
                ops.extend(planned)
                evidence.append(dict(source_id=source.source_id,fact_key=fact['key'],issue_id=issue['issue_id'],order_ids=[op['order_id'] for op in planned]))
            except ValueError as exc:
                pending.append(dict(code='feedback_needs_detail',message=f"{issue.get('guest','')} {issue.get('room','')}：{exc} 同事回复：{answer[:200]}",source_id=source.source_id,fact_key=fact['key']))
    # Conflicting answers for the same order must not execute by upload order.
    grouped={}
    for op in ops:grouped.setdefault(op['order_id'],[]).append(op)
    conflicts={key for key,values in grouped.items() if len({digest(op) for op in values})>1}
    if conflicts:
        ops=[op for op in ops if op['order_id'] not in conflicts]
        evidence=[item for item in evidence if not conflicts.intersection(item['order_ids'])]
        pending.append(dict(code='feedback_conflict',message=f'{len(conflicts)} 笔订单有不同回填答案，请先确认采用哪个答案。',source_id=None,fact_key=None))
    ops=list({digest(op):op for op in ops}.values())
    source_conflicts={c['fact_key'] for c in changes if len({digest(v) for v in changes if v['fact_key']==c['fact_key']})>1}
    if source_conflicts:
        changes=[c for c in changes if c['fact_key'] not in source_conflicts]
        evidence=[e for e in evidence if e.get('changed_fact_key') not in source_conflicts]
        pending.append(dict(code='feedback_conflict',message='同一条来源有不同回填答案，请先统一后重新上传。',source_id=None,fact_key=None))
    changes=list({digest(c):c for c in changes}.values())
    if not ops and not changes:return dict(proposal=None,issues=pending,details=[])
    ids=sorted({o['order_id'] for o in ops}|{o['target_order_id'] for o in ops if o.get('target_order_id')})
    months=affected_months(ctx,cycle,ids)
    owners=sorted({r.owner_id for key,r in ctx['room_map'].items() if r.owner_id and any(leg.room_id==key and leg.order_id in ids for leg in ctx['rooms'])})
    months=sorted(set(months)|{c['decision']['business_month'] for c in changes if c['decision'].get('business_month')})
    payload=dict(type='business_correction',operations=ops,source_changes=changes,order_ids=ids,owner_ids=owners,
        selection={'months':months},reason=text[:1000],details=details(ctx,ops)+[{k:v for k,v in d.items() if k!='fact_key'} for d in source_details if d['fact_key'] not in source_conflicts],feedback_evidence=evidence,
        source_versions=bound_sources)
    proposal=FinancialCaseProposal(proposal_id='FCP-'+uuid4().hex[:20].upper(),cycle_id=cycle.cycle_id,
        run_id=run_id,created_by=actor_id(actor),snapshot_hash=snapshot(ctx,ids),payload=payload,status='pending')
    db.add(proposal);await db.flush()
    return dict(proposal=proposal,issues=pending,details=payload['details'])
