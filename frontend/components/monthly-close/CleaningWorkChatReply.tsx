"use client";
import type { AssistantReply, CleaningWorkComparison, MonthlyCloseProjection } from "@/lib/monthly-close";
import styles from "./CleaningWorkChatReply.module.css";

type ChatAction = { kind: string; room_ref: string; service_date: string; service_type?: string; record_id?: string; confirmed_count?: number; amount?: string; unit_price?: string; existing_amount?: string; quantity?: number; payer?: string; related_fees_unchanged?: Array<{ expense_id: string; amount: string }> };
type ChatFacts = { kind: string; document_id?: string; billing_month: string; state: string; comparison?: CleaningWorkComparison; actions: ChatAction[]; removal_ids: string[]; fee_summary?: { unresolved: Array<{ room_ref: string; service_date: string; reason: string }> } };
const kindLabels: Record<string, string> = { delete: "删除系统记录", add: "补齐历史记录", resolve: "核实重复次数", restore: "恢复已删除记录", fee: "补记保洁费用" };
const differenceLabels: Record<string, string> = { system_only: "表里没有，系统多出", table_only: "表里有，系统缺少", duplicate: "同一天同房有重复", not_completed: "缺少完成依据", unknown_room: "房号还没有对应上" };
export function CleaningWorkChatReply({ reply, projection, onFollowUp, busy, historical = false }: { reply: AssistantReply; projection: MonthlyCloseProjection; onFollowUp?: (text: string, runId: string) => void; busy?: boolean; historical?: boolean }) {
  if (reply.tool !== "cleaning_work_chat" || projection.actor_role !== "admin") return null;
  const facts = reply.facts as unknown as ChatFacts;
  if (facts.billing_month !== projection.billing_month || (facts.document_id && !projection.sources.some((source) => source.documents.some((doc) => doc.document_id === facts.document_id)))) return null;
  const follow = (text: string) => onFollowUp?.(text, reply.run_id);
  const report = facts.comparison;
  return <div className={styles.reply} aria-label="保洁聊天核对与处理">
    {facts.actions?.length > 0 && <>
      <p className={styles.caption}>{facts.state === "completed" ? "这次已执行" : historical ? "历史修改清单" : "待你确认的修改清单"}</p>
      <details open={facts.actions.length <= 8}><summary>查看 {facts.actions.length} 项具体修改</summary><ol className={styles.list}>{facts.actions.map((action, index) => <li key={`${index}-${action.record_id ?? action.room_ref}`}>
        <div>{action.service_date} · {action.room_ref} · {action.service_type === "instay_cleaning" ? "续住打扫" : "正常打扫"}</div>
        <div>{kindLabels[action.kind] ?? action.kind}{action.confirmed_count != null ? `，保留 ${action.confirmed_count} 次` : ""}</div>
        {action.kind === "fee" && <p>{action.quantity} 次 × ¥{action.unit_price}，已有 ¥{action.existing_amount}，本次补记 ¥{action.amount}。{action.payer === "company" ? "公司承担" : "业主承担"}。</p>}
        {action.record_id && <small>关联上述房间的{action.service_type === "instay_cleaning" ? "续住打扫" : "正常打扫"}记录</small>}
        {!!action.related_fees_unchanged?.length && <p>关联费用 {action.related_fees_unchanged.map((fee) => `¥${fee.amount}`).join("、")} 仍保留；本次只修改打扫记录。</p>}
      </li>)}</ol></details>
    </>}
    {facts.state === "proposal" && historical && <p className={styles.caption}>历史方案，已被后续处理替代。请查看最新回复。</p>}
    {facts.state === "proposal" && !historical && <div className={styles.actions}>
      <button type="button" className={styles.primary} disabled={busy || !onFollowUp} onClick={() => follow("确认执行")}>确认执行这份方案</button>
      <button type="button" disabled={busy || !onFollowUp} onClick={() => follow("取消方案")}>先不执行</button>
      <span>也可以直接在下方聊天框修改要求。</span>
    </div>}
    {facts.state === "completed" && facts.removal_ids?.length > 0 && <div className={styles.actions}><button type="button" disabled={busy || !onFollowUp} onClick={() => follow("恢复刚才删除的记录")}>恢复这次删除的记录</button></div>}
    {!!facts.fee_summary?.unresolved.length && <details open><summary>还有 {facts.fee_summary.unresolved.length} 项费用需核实</summary><ol className={styles.list}>{facts.fee_summary.unresolved.map((item, index) => <li key={index}>{item.service_date} · {item.room_ref}：{item.reason}</li>)}</ol></details>}
    {report && <details open={!facts.fee_summary && (facts.state === "report" || facts.state === "conflict")}>
      <summary>核对结果：已对应 {report.matched_count} 次，剩余 {report.differences.length} 项</summary>
      <p>原表 {report.record_count} 条 · 核实后有效 {report.effective_record_count ?? report.record_count} 次 · 已补齐 {report.recorded_count ?? 0} 次</p>
      <ol className={styles.list}>{report.differences.map((item, index) => <li key={`${index}-${item.room_ref}`}>
        <div>{item.service_date} · {item.room_ref} · {item.service_type === "instay_cleaning" ? "续住打扫" : "正常打扫"}</div>
        <div>{differenceLabels[item.status] ?? item.status}：原表 {item.table_count} 次，系统 {item.system_count} 条</div>
        {item.source_rows.length > 0 && <small>原表第 {item.source_rows.join("、")} 行</small>}
        <div className={styles.actions}>
          {!historical && item.table_count === 0 && <button disabled={busy} onClick={() => follow(`只删除 ${item.service_date} 房间 ${item.room_ref} 的${item.service_type === "instay_cleaning" ? "续住打扫" : "正常打扫"}系统多余记录`)}>按表删除这项</button>}
          {!historical && item.status === "duplicate" && item.table_count > 1 && <>
            <button disabled={busy} onClick={() => follow(`${item.service_date} 房间 ${item.room_ref} 重复填写，只算一次`)}>实际只有一次</button>
            <button disabled={busy} onClick={() => follow(`${item.service_date} 房间 ${item.room_ref} 重复项按原表次数保留`)}>确实按表打扫多次</button>
          </>}
        </div>
      </li>)}</ol>
    </details>}
    {!historical && facts.state === "report" && report && report.differences.length > 0 && <div className={styles.actions}>
      <button disabled={busy || !onFollowUp} onClick={() => follow("按表补齐，并删除系统多余的打扫记录")}>按表生成处理方案</button>
    </div>}
    {!historical && !facts.fee_summary && ["report", "completed"].includes(facts.state) && report && report.differences.length === 0 && (report.recorded_count ?? 0) > 0 && <div className={styles.actions}>
      <button disabled={busy || !onFollowUp} onClick={() => follow("核对并补齐续住保洁费用")}>继续核对续住保洁费用</button>
      <span>打扫记录已对上，费用还需核对。</span>
    </div>}
  </div>;
}
