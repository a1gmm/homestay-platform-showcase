"""Evidence-driven progression of an entire month, without implicit approvals.

Each pass discovers every currently actionable domain, prepares immutable plans,
and re-reads real execution outcomes on the next pass. The language-model output
is never a source of monetary values, permissions or completion claims.
"""
from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.models.financial_case import FinancialCaseProposal, FinancialCaseSource
from app.models.monthly_close import MonthlyCloseDocument, uses_legacy_utility_contract
from app.models.monthly_close_control import MonthlyCloseProposal, MonthlyCloseRun
from app.models.room import Room
from app.services.financial_case import service as case_service
from app.services.monthly_close import adapters, utility_source
from app.services.monthly_close.cleaning_work_chat import ChatSpec, WorkChatFacts, VERSION as WORK_VERSION, build_plan, digest
from app.services.monthly_close.cleaning_work_log import parse_cleaning_work_log
from app.services.monthly_close.control import MonthlyCloseControlError
from app.services.monthly_close.task_chat import persist_result
from app.services.monthly_close.task_delivery import verify_delivery
from app.services.monthly_close.workflow import MonthlyCloseConflict


def _question(key, subject, message, **extra):
    return {"key": key, "subject": subject, "message": message, **extra}


def _chat_action(reply, label):
    return {"key": reply.run_id, "label": label, "text": "查看任务方案", "context_run_id": reply.run_id}


async def _cleaning(db, cycle, actor, checkpoint):
    actions, questions, fingerprints = [], [], []
    documents = list(await db.scalars(select(MonthlyCloseDocument).where(
        MonthlyCloseDocument.cycle_id == cycle.cycle_id,
        MonthlyCloseDocument.source_type == "cleaning_statement",
        MonthlyCloseDocument.is_active.is_(True)).options(undefer(MonthlyCloseDocument.content))
        .order_by(MonthlyCloseDocument.uploaded_at, MonthlyCloseDocument.document_id)))
    for document in documents:
        if not document.content or len(document.content) != document.byte_size or sha256(document.content).hexdigest() != document.sha256:
            questions.append(_question(f"original:{document.document_id}", document.filename,
                "原件内容与保存记录不一致，不能用来生成补齐方案。请重新上传并核对。", step_key="source_collection"))
            continue
        try:
            parsed = parse_cleaning_work_log(document.content, document.filename, cycle.billing_month)
        except ValueError as exc:
            raise MonthlyCloseConflict("cleaning_original_invalid", "保洁原表的日期或房号未能完整识别，请打开识别结果核对原表。") from exc
        if not parsed:
            continue  # Supplier cost statements use the separate source adapter.
        reason = "依据本月已保存保洁原表与当前系统记录核对，先准备确定性缺项；歧义另列核实。"
        spec = ChatSpec(add_missing=True)
        plan = await build_plan(db, cycle, document, actor["user_id"], spec, reason)
        fingerprints.append(plan["preview_hash"])
        differences = plan["comparison"]["differences"]
        if not differences:
            spec = ChatSpec(repair_fees=True)
            plan = await build_plan(db, cycle, document, actor["user_id"], spec, reason)
            fingerprints.append(plan["preview_hash"])
        unresolved = (plan.get("fee_summary") or {}).get("unresolved", [])
        for i, item in enumerate(unresolved):
            questions.append(_question(f"cleaning-fee:{document.document_id}:{digest(item)}", "续住保洁费用",
                str(item.get("message") or item.get("reason") or "该次打扫未能确定费用依据，请核对关联订单和费用标准。"),
                step_key="service_fees"))
        labels = {"system_only": "系统有记录，原表没有，请核实是否删除系统记录。",
                  "duplicate": "同日同房有重复记录，请核实实际次数。",
                  "unknown_room": "原表房号未能对应系统，请核实房号。"}
        add_keys = {(a.get("service_date"), a.get("room_ref"), a.get("service_type"))
                    for a in plan["actions"] if a.get("kind") == "add"}
        for row in differences:
            identity = (row["service_date"], row["room_ref"], row["service_type"])
            if identity in add_keys:
                continue
            questions.append(_question(f"cleaning:{document.document_id}:{digest(identity)}",
                f"{row['service_date']} · {row['room_ref']} · {'续住打扫' if row['service_type'] == 'instay_cleaning' else '正常打扫'}",
                f"{document.filename} 原表 {row['table_count']} 次、系统 {row['system_count']} 条。"
                + labels.get(row["status"], "请核实原表日期、房间和实际打扫次数。")
                + (f" 原表第 {'、'.join(map(str, row['source_rows']))} 行。" if row.get("source_rows") else ""),
                step_key="service_fees"))
        # The established cleaning confirmation contract permits only the latest
        # plan, so prepare one file at a time while still collecting all issues.
        if plan["actions"] and not actions and cycle.status != "completed":
            message = (f"已准备补记 {len(plan['actions'])} 笔续住保洁费用，请核对后确认。"
                       if spec.repair_fees else f"已准备按原表补齐 {len(plan['actions'])} 条打扫记录，请核对后确认。")
            facts = WorkChatFacts(document_id=document.document_id, filename=document.filename,
                billing_month=cycle.billing_month, state="proposal", message=message,
                comparison=plan["comparison"], spec=spec, preview_hash=plan["preview_hash"],
                actions=plan["actions"], reason=reason, request_text="整月任务准备方案",
                fee_summary=plan.get("fee_summary")).model_dump()
            reply = await persist_result(db, cycle, actor,
                key=f"{checkpoint['task_id']}:cleaning:{plan['preview_hash']}",
                tool="cleaning_work_chat", facts=facts, message=message, manifest=WORK_VERSION)
            latest = await db.scalar(select(MonthlyCloseRun).where(
                MonthlyCloseRun.cycle_id == cycle.cycle_id, MonthlyCloseRun.actor_id == actor["user_id"],
                MonthlyCloseRun.tool_manifest_version == WORK_VERSION, MonthlyCloseRun.status == "succeeded")
                .order_by(MonthlyCloseRun.started_at.desc(), MonthlyCloseRun.run_id.desc()).limit(1))
            if latest and latest.run_id != reply.run_id:
                questions.append(_question("cleaning:revised-plan", "保洁处理方案",
                    "你已在聊天中修改或取消了方案。请处理聊天中最新方案，任务会在记录改变后继续。", step_key="service_fees"))
            else:
                actions.append(_chat_action(reply, "核对续住费用方案" if spec.repair_fees else "核对打扫补齐方案"))
    return actions, questions, fingerprints


async def _financial(db, cycle, actor, checkpoint):
    sources = await case_service.load_sources(db, cycle.cycle_id)
    if not sources:
        return [], [], None
    originals = dict((await db.execute(select(FinancialCaseSource.source_id, FinancialCaseSource.content)
        .where(FinancialCaseSource.source_id.in_([s.source_id for s in sources])))).all())
    damaged = [s for s in sources if not originals.get(s.source_id)
               or sha256(originals[s.source_id]).hexdigest() != s.sha256]
    if damaged:
        return [], [_question(f"original:{s.source_id}", s.filename,
            "原件内容与保存时不一致，请重新上传核对后再生成费用方案。", step_key="source_collection")
            for s in damaged], digest([s.source_id for s in damaged])
    matched = await case_service.match_case(db, cycle, sources, [cycle.billing_month])
    by_source = {s.source_id: s.filename for s in sources}
    questions = [_question(f"case:{i['code']}:{i.get('fact_key', digest(i))}",
        by_source.get(i.get("source_id"), "原件与账本核对"), i["message"],
        code=i["code"], step_key="utilities") for i in matched["issues"]]
    operations = matched["operations"][:500]
    if not operations or cycle.status == "completed":
        return [], questions, matched["snapshot_hash"]
    key = f"{checkpoint['task_id']}:financial:{matched['snapshot_hash']}:{digest(operations)}"
    total = sum((Decimal(op["amount"]) for op in operations), Decimal(0))
    message = f"已准备 {len(operations)} 笔依据明确的费用，共 {total:.2f} 元。请核对承担方、房间和原表行后确认；其他问题单独保留。"

    async def prepare(run_id):
        proposal = FinancialCaseProposal(proposal_id="FCP-" + uuid4().hex[:20].upper(),
            cycle_id=cycle.cycle_id, run_id=run_id, created_by=actor["user_id"],
            snapshot_hash=matched["snapshot_hash"], status="pending",
            payload={"operations": operations, "selection": {"months": [cycle.billing_month],
                "source_ids": [], "categories": [], "fact_keys": [op["fact_key"] for op in operations]}})
        db.add(proposal)
        await db.flush()
        rooms = dict((await db.execute(select(Room.room_id, Room.room_name))).all())
        evidence = {(s.source_id, f["key"]): f for s in sources for f in s.parsed.get("facts", [])}
        details = []
        from app.models.expense import ExpenseCategory
        from app.services.financial_case.service import EXPENSE_CATEGORY_LABELS
        for op in operations:
            fact = evidence[(op["source_id"], op["fact_key"])]
            label = f"{op['expense_date']} · {rooms.get(op.get('room_id'), '公共费用')} · {EXPENSE_CATEGORY_LABELS[ExpenseCategory(op['category'])]}"
            detail = f"{op['amount']} 元，{'公司' if op['payer'] == 'company' else '业主'}承担；来源：{by_source[op['source_id']]} · {fact.get('sheet', '原表')} 第 {fact.get('row1based', fact.get('row'))} 行。"
            details.append({"label": label, "value": detail})
        return {"projection_version": "financial-case-v1", "billing_month": cycle.billing_month,
            "state": "proposal", "message": message,
            "sources": [{"source_id": s.source_id, "filename": s.filename, "kind": s.kind,
                         "fact_count": len(s.parsed.get("facts", []))} for s in sources][:100],
            "metrics": [], "issues": [], "details": details,
            "proposal_id": proposal.proposal_id, "proposal_kind": "posting",
            "report_months": [cycle.billing_month], "export_ready": False}

    reply = await persist_result(db, cycle, actor, key=key, tool="financial_case", facts={},
                                  message=message, manifest="financial-case-v1", prepare=prepare)
    proposal = await db.get(FinancialCaseProposal, reply.facts["proposal_id"])
    if proposal.status == "pending":
        return [_chat_action(reply, "核对原件费用入账方案")], questions, matched["snapshot_hash"]
    questions.append(_question("case:previous-plan", "费用处理方案",
        "之前的费用方案已处理或取消，请说明新的处理要求后继续；不会自动重复提交相同方案。", step_key="utilities"))
    return [], questions, matched["snapshot_hash"]


async def _ota(db, cycle, actor, checkpoint):
    """Prepare only linked, deterministic platform corrections; no auto appeals."""
    from app.models.recon import ReconBatch, ReconDiff, ReconDiffClass, ReconDiffStatus
    actions, questions, fingerprints = [], [], []
    # Retain primitive batch ids only: a rejected batch rolls back and expires
    # ORM objects, so attached originals must be read anew for each iteration.
    batches = set(await db.scalars(select(MonthlyCloseDocument.engine_id).where(
        MonthlyCloseDocument.cycle_id == cycle.cycle_id,
        MonthlyCloseDocument.source_type == "ota_statement",
        MonthlyCloseDocument.engine_type == "billing_recon",
        MonthlyCloseDocument.engine_id.is_not(None),
        MonthlyCloseDocument.is_active.is_(True))))
    for batch_id in sorted(batches):
        batch = await db.get(ReconBatch, batch_id)
        if batch is None or batch.bill_month != cycle.billing_month or batch.status != "parsed" or (batch.mapping or {}).get("archived_at"):
            continue
        attached = (await db.execute(select(MonthlyCloseDocument.document_id,
            MonthlyCloseDocument.content, MonthlyCloseDocument.sha256, MonthlyCloseDocument.byte_size).where(
            MonthlyCloseDocument.cycle_id == cycle.cycle_id,
            MonthlyCloseDocument.source_type == "ota_statement",
            MonthlyCloseDocument.engine_type == "billing_recon",
            MonthlyCloseDocument.engine_id == batch_id,
            MonthlyCloseDocument.is_active.is_(True)).order_by(MonthlyCloseDocument.document_id))).all()
        if not attached or any(not d.content or len(d.content) != d.byte_size
                               or sha256(d.content).hexdigest() != d.sha256 for d in attached):
            questions.append(_question(f"ota:original:{batch_id}", "平台账单原件", "平台原件内容与保存记录不一致，请重新上传核对。", step_key="ota_statements"))
            continue
        diffs = list(await db.scalars(select(ReconDiff).where(ReconDiff.batch_id == batch_id,
            ReconDiff.status == ReconDiffStatus.pending).order_by(ReconDiff.diff_id)))
        selected = [d.diff_id for d in diffs if d.diff_class in {ReconDiffClass.fix_amount, ReconDiffClass.compensation}]
        if not selected or cycle.status == "completed":
            continue
        fingerprint = digest({"batch": batch_id, "mapping": batch.mapping,
            "originals": [{"id": d.document_id, "sha256": d.sha256} for d in attached],
            "diffs": [{"id": d.diff_id, "order_id": d.order_id,
                "class": d.diff_class.value, "amount": str(d.bill_amount),
                "system_amount": str(d.system_amount), "status": d.status.value,
                "detail": d.detail} for d in diffs if d.diff_id in selected]})
        fingerprints.append(fingerprint)
        try:
            proposal = await adapters.build_ota_proposal(db, cycle.cycle_id, batch_id, selected, actor,
                request_id=f"task:{checkpoint['task_id']}:ota:{fingerprint[:24]}")
            await db.commit()
            if proposal.status in {"rejected", "stale", "superseded"}:
                questions.append(_question(f"ota:rejected:{batch_id}", "平台账单处理方案", "之前的方案已拒绝或失效，请核对当前依据并说明需要调整的处理方式。", step_key="ota_statements"))
            else:
                actions.append({"key": f"proposal:{proposal.proposal_id}", "label": "核对平台金额更正方案", "text": "", "proposal_id": proposal.proposal_id})
        except (MonthlyCloseConflict, MonthlyCloseControlError) as exc:
            await db.rollback()
            await db.refresh(cycle)
            questions.append(_question(f"ota:plan:{batch_id}", "平台账单核对", exc.message, step_key="ota_statements"))
    return actions, questions, fingerprints


async def advance_task(db, cycle, actor, checkpoint):
    from app.services.monthly_close.permissions import MonthlyCloseFeature, assert_monthly_close_feature_enabled
    # Runtime also rechecks the active administrator before and after advancing.
    assert_monthly_close_feature_enabled(MonthlyCloseFeature.natural_language, actor)
    from app.services.monthly_close.task_chat import has_unsupported_task_scope
    if has_unsupported_task_scope(checkpoint.get("goal", "")):
        return {"status": "waiting_user", "checkpoint": {**checkpoint, "delivery_verified": False},
            "summary": "这项要求包含单独范围或业务规则，请先在聊天中确认具体处理，再开始整月核对。",
            "questions": [_question("task:scope", "任务范围", "单项核对、费用承担方、删除或忽略要求需要具体方案，不能作为整月任务的默认规则。")],
            "actions": [], "artifacts": [], "steps": [], "evidence_hash": ""}
    actions, questions, fingerprints = [], [], []
    stage_errors = []
    # A closed month is still eligible for read-only delivery verification.
    # Preparation helpers deliberately reject closed months; do not invoke
    # them merely to inspect already completed accountant deliverables.
    handlers = () if cycle.status == "completed" else (
        ("保洁记录与费用", _cleaning), ("原件费用", _financial), ("平台金额", _ota))
    for name, handler in handlers:
        try:
            extra_actions, extra_questions, hashes = await handler(db, cycle, actor, checkpoint)
            actions.extend(extra_actions); questions.extend(extra_questions); fingerprints.append(hashes)
        except (MonthlyCloseConflict, MonthlyCloseControlError) as exc:
            # Domain ambiguity is a human question; unexpected service failures
            # propagate to the durable bounded retry mechanism instead.
            await db.rollback()
            await db.refresh(cycle)
            questions.append(_question(f"stage:{name}", name, str(getattr(exc, "message", "原件结构或业务依据需要核对，请打开对应资料查看。")), step_key="service_fees" if name.startswith("保洁") else "utilities"))
            stage_errors.append(name)

    if cycle.status != "completed":
        active_documents = list(await db.scalars(select(MonthlyCloseDocument).where(
            MonthlyCloseDocument.cycle_id == cycle.cycle_id, MonthlyCloseDocument.is_active.is_(True))))
        for key, label, reader, builder in (
            ("service", "订单服务费", adapters.adapt_service_source, adapters.build_service_proposal),
            ("utility", "水电费用", utility_source.adapt_utility_source, utility_source.build_utility_proposal),
            ("operating", "运营支出", adapters.adapt_operating_expense_source, adapters.build_operating_expense_proposal),
        ):
            if key == "utility" and not uses_legacy_utility_contract(cycle, documents=active_documents):
                continue
            adapter = await reader(db, cycle)
            fingerprint = digest(adapter.model_dump(mode="json"))
            fingerprints.append(fingerprint)
            for issue in adapter.issues:
                if not issue.command_key:
                    questions.append(_question(f"adapter:{issue.issue_key}", label, issue.message,
                        code=issue.code, step_key="service_fees" if key == "service" else "utilities"))
            if not adapter.commands:
                continue
            cache = (checkpoint.get("proposals") or {}).get(key, {})
            proposal = await db.get(MonthlyCloseProposal, cache.get("proposal_id")) if cache.get("fingerprint") == fingerprint else None
            if proposal and proposal.status == "rejected":
                questions.append(_question(f"proposal:{key}:rejected", label, "你已拒绝这份方案，请在聊天中说明要调整的业务事实；不会重复提交相同方案。"))
                continue
            if proposal is None or proposal.status in {"stale", "superseded"}:
                proposal = await builder(db, cycle.cycle_id, actor,
                    request_id=f"task:{checkpoint['task_id']}:{key}:{fingerprint[:24]}")
                await db.commit()
            if proposal.status in {"rejected", "stale", "superseded"}:
                questions.append(_question(f"proposal:{key}:unavailable", label,
                    "之前的方案已拒绝或失效，请补充新的业务依据后继续；不会重新提交同一份方案。"))
                continue
            checkpoint["proposals"] = {**checkpoint.get("proposals", {}), key: {"fingerprint": fingerprint, "proposal_id": proposal.proposal_id}}
            actions.append({"key": f"proposal:{proposal.proposal_id}", "label": f"核对{label}方案", "text": "", "proposal_id": proposal.proposal_id})

    delivery = await verify_delivery(db, cycle, actor)
    questions.extend(delivery["questions"])
    # Generate the owner bills only after all upstream factual gates pass. No
    # draft/confirmed bill is overwritten without its own reviewed proposal.
    checks = {c["key"]: c for c in delivery["checks"]}
    upstream = [c for c in delivery["checks"] if c["key"].startswith("step:") and c["key"] != "step:settlement_review"]
    incomplete_coverage = checks.get("settlement_coverage", {}).get("status") == "blocked"
    missing_bills = checks.get("settlements_present", {}).get("status") == "blocked" or incomplete_coverage
    stale_packages = any(c["key"].startswith("package:") and c["status"] == "blocked" for c in delivery["checks"])
    rebuild_pending = stale_packages or incomplete_coverage
    if not actions and not stage_errors and cycle.status != "completed" and upstream and all(c["status"] == "passed" for c in upstream) and (missing_bills or stale_packages):
        from app.services.monthly_close.finalization import build_settlement_proposal
        try:
            from app.api.v1.settlements import build_settlement_plan_snapshot
            year, month = map(int, cycle.billing_month.split("-"))
            planned = await build_settlement_plan_snapshot(db, year, month, overwrite=rebuild_pending)
            if not planned["settlements"]:
                raise MonthlyCloseConflict("settlement_no_unlocked_targets",
                    "现有结算单已锁定，不能直接重算覆盖。请核对差额并通过结算更正流程处理。")
            proposal = await build_settlement_proposal(db, cycle, actor,
                request_id=f"task:{checkpoint['task_id']}:settle:{delivery['evidence_hash'][:24]}", overwrite=rebuild_pending)
            await db.commit()
            if proposal.status in {"rejected", "stale", "superseded"}:
                questions.append(_question("settlement:rejected", "业主结算方案", "之前的结算方案已拒绝或失效，请在聊天中说明需要调整的业务依据。", step_key="settlement_review"))
            else:
                actions.append({"key": f"proposal:{proposal.proposal_id}", "label": "核对业主结算生成方案", "text": "", "proposal_id": proposal.proposal_id})
        except (MonthlyCloseConflict, MonthlyCloseControlError) as exc:
            await db.rollback()
            questions.append(_question("settlement:prepare", "业主结算生成", exc.message, step_key="settlement_review"))
        except HTTPException as exc:
            if exc.status_code not in {409, 422}:
                raise
            await db.rollback()
            detail = exc.detail.get("message") if isinstance(exc.detail, dict) else exc.detail
            questions.append(_question("settlement:precondition", "业主结算生成", str(detail), step_key="preflight"))
    questions = list({q["key"]: q for q in questions}.values())
    ready = delivery["status"] == "ready" and not actions and not questions and not stage_errors
    checkpoint["delivery_verified"] = ready
    checkpoint["evidence_hash"] = delivery["evidence_hash"]
    summary = (delivery["summary"] if ready else
        f"已检查本月资料与账本，准备了 {len(actions)} 份待审核方案，另有 {len(questions)} 项资料或事实需要处理。确认或补充后会继续核对，直到交付核验通过。")
    return {"status": "succeeded" if ready else "waiting_approval" if actions else "waiting_user",
        "checkpoint": checkpoint, "summary": summary,
        "steps": [{"key": c["key"], "label": c["label"], "detail": c["detail"],
                   "status": "completed" if c["status"] == "passed" else c["status"]} for c in delivery["checks"]],
        "questions": questions, "actions": actions, "artifacts": delivery["artifacts"],
        "evidence_hash": digest([delivery["evidence_hash"], fingerprints])}
