"use client";

import { useState } from "react";
import { Drawer } from "antd";
import { SystemServiceFeeReview } from "./SystemServiceFeeReview";
import type { AssistantReply, CleaningInvestigationFacts, MonthlyCloseProjection } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";

const buttonStyle = { minHeight: 44, borderRadius: 999, padding: "0 16px", border: `1px solid ${tokens.anyu.color.linen}`, background: tokens.anyu.color.shell, color: tokens.anyu.color.ink.default, font: "inherit" } as const;
const noteLabels = { extra_cleaning: "员工说明：曾额外安排保洁", replacement_statement: "员工说明：这是修订后的完整账单", service_not_performed: "员工说明：这次保洁没有发生" };
const taskStatus: Record<string, string> = { done: "已完成", pending: "待处理", in_progress: "进行中", cancelled: "已取消" };
const reviewStatus: Record<string, string> = { approved: "已审核", rejected: "审核未通过", pending_review: "待审核" };

export function CleaningInvestigationReply({ reply, projection, onFollowUp, onSelectDocument, busy = false }: {
  reply: AssistantReply;
  projection: MonthlyCloseProjection;
  onFollowUp?: (text: string, contextRunId: string) => void;
  onSelectDocument?: (documentId: string) => void;
  busy?: boolean;
}) {
  const [reviewOpen, setReviewOpen] = useState(false);
  if (reply.tool !== "investigate_cleaning" || reply.facts.kind !== "cleaning_investigation") return null;
  // Mirror the existing financial-detail boundary, including after role changes.
  if (!["admin", "finance"].includes(projection.actor_role)) return null;
  const facts = reply.facts as unknown as CleaningInvestigationFacts;
  if (facts.billing_month !== projection.billing_month || !Array.isArray(facts.cases)) return null;
  const documents = projection.sources.flatMap((source) => source.documents);
  const visibleIds = new Set(documents.map((document) => document.document_id));
  const followUp = (text: string) => onFollowUp?.(text, reply.run_id);
  const selected = facts.selected;
  const selectedAvailable = selected && visibleIds.has(selected.document_id);
  return (
    <div aria-label="保洁差异调查依据" style={{ display: "grid", gap: 12, marginTop: 12, overflowWrap: "anywhere" }}>
      <div style={{ color: tokens.anyu.color.stone }}>这是本次查询的证据快照；记录变化后请重新核查。</div>
      {facts.cases.length > 0 && <ol style={{ margin: 0, paddingLeft: 24 }}>
        {facts.cases.map((item) => !visibleIds.has(item.document_id) ? <li key={item.case_id} value={item.ordinal}>这笔明细的资料已不可用，请重新查保洁差异。</li> : <li key={item.case_id} value={item.ordinal} style={{ marginBottom: 12 }}>
          <div>{item.service_date ?? "日期待核实"} · {item.room_id ?? "房间待核实"}</div>
          <div>{item.summary}</div>
          <button type="button" style={buttonStyle} disabled={busy || !onFollowUp || !visibleIds.has(item.document_id)} onClick={() => followUp(`查看第${item.ordinal}笔保洁差异`)}>查看第{item.ordinal}笔依据</button>
        </li>)}
      </ol>}
      {selected && !selectedAvailable && <div role="status">这笔明细的资料已不可用，请重新查保洁差异。</div>}
      {selectedAvailable && <>
        {selected.detail_limited && <div role="status">关联记录较多，下面只展示部分依据，不能据此认定所有记录已查完。</div>}
        <div>第{selected.ordinal}笔 · {selected.service_date} · {selected.room_id ?? "房间待核实"}</div>
        <div>{selected.summary}</div>
        <dl style={{ margin: 0, display: "grid", gap: 8 }}>
          <div><dt>供应商金额</dt><dd style={{ margin: 0 }}>¥{selected.vendor_amount}</dd></div>
          <div><dt>系统金额</dt><dd style={{ margin: 0 }}>{selected.system_amount === null ? "尚未唯一匹配" : `¥${selected.system_amount}`}</dd></div>
          {selected.difference !== null && <div><dt>差额（供应商 − 系统）</dt><dd style={{ margin: 0 }}>¥{selected.difference}</dd></div>}
        </dl>
        <details>
          <summary style={{ minHeight: 44, cursor: "pointer", display: "flex", alignItems: "center" }}>查看原表行与系统记录</summary>
          <div style={{ display: "grid", gap: 12 }}>
            <div>原表依据</div>
            {selected.sources.map((source) => <div key={source.line_id}>
              <div>{documents.find((document) => document.document_id === source.document_id)?.filename ?? "保洁账单"} · 已确认的工作表 · 第 {source.row_number} 行 · ¥{source.amount}</div>
              <button type="button" style={buttonStyle} disabled={!onSelectDocument || !visibleIds.has(source.document_id)} onClick={() => onSelectDocument?.(source.document_id)}>查看这份文件</button>
            </div>)}
            {selected.related_sources.length > 0 && <div>其他有效文件中的相同业务明细</div>}
            {selected.related_sources.filter((source) => visibleIds.has(source.document_id)).map((source) => <div key={source.line_id}>
              <div>{documents.find((document) => document.document_id === source.document_id)?.filename ?? "保洁账单"} · 已确认的工作表 · 第 {source.row_number} 行 · ¥{source.amount}</div>
              <button type="button" style={buttonStyle} disabled={!onSelectDocument} onClick={() => onSelectDocument?.(source.document_id)}>查看相关文件</button>
            </div>)}
            <div>当前系统费用</div>
            {selected.expenses.length ? selected.expenses.map((expense) => <div key={expense.expense_id}>{expense.expense_date} · {expense.expense_id} · ¥{expense.amount}</div>) : <div>未找到可唯一匹配的系统服务费。</div>}
            <div>关联订单与续住记录</div>
            {selected.orders.length ? selected.orders.map((order, index) => <div key={`${order.order_id}-${index}`}>{order.order_id} · {order.room_id ?? "待排房"} · {order.check_in} → {order.check_out}{order.stay_group_id ? " · 存在续住关联" : ""}{order.relationship === "room_date_candidate" ? " · 同房同日期候选，尚未证实对应本次保洁" : ""}</div>) : <div>尚未找到关联订单；不能据此认定没有入住或保洁。</div>}
            <div>关联保洁任务</div>
            {selected.tasks.length ? selected.tasks.map((task) => <div key={task.task_id}>{task.task_id} · {taskStatus[task.status] ?? "状态待核实"} · {reviewStatus[task.review_status ?? ""] ?? "未记录审核结果"}</div>) : <div>未找到关联保洁任务；需要补充服务记录。</div>}
            <div style={{ color: tokens.anyu.color.stone }}>关联记录用于调查，任务完成状态本身不能证明这笔费用应如何调整。</div>
          </div>
        </details>
        {facts.investigation_steps && facts.investigation_steps.length > 0 && <div style={{ color: tokens.anyu.color.stone }}>本次调查：{facts.investigation_steps.map((step) => ({ read_amounts: "核对金额", read_sources: "核对原件", read_orders: "查订单与续住", read_tasks: "查保洁任务" }[step] ?? "核对依据")).join(" → ")}</div>}
        {facts.reasoning_mode === "fallback" && <div>模型本次未完成调查，以下结论来自系统已核实的记录，可继续核对。</div>}
        {!!facts.findings?.length && <div aria-label="调查结论" style={{ display: "grid", gap: 10 }}>
          {facts.findings.map((finding) => <div key={finding.id}>
            <div>{finding.text}</div>
            <div style={{ fontSize: 12, color: tokens.anyu.color.stone }}>{finding.evidence_refs.map((ref) => {
              const [kind, ...parts] = ref.split(":");
              const id = parts.join(":");
              const row = [...selected.sources, ...selected.related_sources].find((source) => source.line_id === id);
              return kind === "source" && row ? `原表第 ${row.row_number} 行` : `${({ expense: "费用", order: "订单", task: "任务" }[kind] ?? "依据")} ${id}`;
            }).join(" · ")}</div>
          </div>)}
        </div>}
        <div>{selected.question}</div>
        {facts.correction_draft && <div aria-label="服务费修正草稿" style={{ display: "grid", gap: 10 }}>
          <div>{facts.correction_draft.message}</div>
          {facts.correction_draft.changes.map((change, index) => <div key={`${change.order_id}-${change.category}-${index}`}>
            {change.order_id} · {change.room_id} · {({ cleaning: "保洁", laundry: "洗涤", daily_supplies: "日耗" }[change.category] ?? "服务费")}：{change.before_amount === null ? "待新增或核对原记录" : `¥${change.before_amount}`} → ¥{change.after_amount}
            <div>费用日期：{change.before_date ?? "待新增"} → {change.after_date}；付款方：{change.before_payer === "owner" ? "业主" : change.before_payer === "company" ? "公司" : "待新增"} → {change.after_payer === "owner" ? "业主" : change.after_payer === "company" ? "公司" : change.after_payer}</div>
          </div>)}
          {facts.correction_draft.state === "ready" && <>
            <div>本月方案预计合计变化：¥{facts.correction_draft.amount_impact}。草稿尚未批准或改账。</div>
            <button type="button" style={buttonStyle} onClick={() => setReviewOpen(true)}>打开方案审核与执行</button>
          </>}
        </div>}
        <Drawer title="服务费修正方案" open={reviewOpen} onClose={() => setReviewOpen(false)} width={520} destroyOnHidden>
          {reviewOpen && <SystemServiceFeeReview key={`${projection.billing_month}-${projection.actor_role}`} billingMonth={projection.billing_month} role={projection.actor_role} onFinished={async () => { followUp("重新核查这笔保洁差异"); }} />}
        </Drawer>
        {facts.note_code && <div role="status">{noteLabels[facts.note_code]}。这仍是待核实线索，尚未补齐凭证或更改费用。</div>}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button type="button" style={buttonStyle} disabled={busy || !onFollowUp} onClick={() => followUp("生成这笔保洁修正草稿")}>生成修正草稿</button>
          <button type="button" style={buttonStyle} disabled={busy || !onFollowUp} onClick={() => followUp("重新查这笔保洁差异")}>重新核查这笔</button>
          <button type="button" style={buttonStyle} disabled={busy || !onFollowUp} onClick={() => followUp("查保洁差异清单")}>返回差异清单</button>
          {facts.note_code && <button type="button" style={buttonStyle} disabled={busy || !onFollowUp} onClick={() => followUp("撤回这条说明")}>撤回这条说明</button>}
        </div>
      </>}
      {!selected && <div style={{ display: "flex", gap: 8 }}>
        {facts.page > 0 && <button type="button" style={buttonStyle} disabled={busy || !onFollowUp} onClick={() => followUp("上一页保洁差异")}>上一页</button>}
        {facts.has_more && <button type="button" style={buttonStyle} disabled={busy || !onFollowUp} onClick={() => followUp("下一页保洁差异")}>下一页</button>}
      </div>}
    </div>
  );
}
