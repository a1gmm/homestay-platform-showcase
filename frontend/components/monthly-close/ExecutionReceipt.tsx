import type { AssistantReply, MonthlyCloseProjection } from "@/lib/monthly-close";

/** Display only facts returned by the executed operation; missing effects stay unknown. */
export function ExecutionReceipt({ reply, projection }: { reply: AssistantReply; projection: MonthlyCloseProjection }) {
  const facts = reply.facts;
  if (facts.state !== "completed" || facts.billing_month !== projection.billing_month || projection.actor_role !== "admin") return null;
  const actions = Array.isArray(facts.actions) ? facts.actions as Array<Record<string, unknown>> : [];
  const fees = actions.filter(action => action.kind === "fee");
  const records = actions.filter(action => ["add", "delete", "resolve", "restore"].includes(String(action.kind)));
  const byPayer = (payer: string) => fees.filter(action => action.payer === payer).length;
  const cents = fees.map(action => {
    const match = String(action.amount ?? "").match(/^(-?)(\d+)(?:\.(\d{1,2}))?$/);
    return match ? (match[1] ? -1 : 1) * (Number(match[2]) * 100 + Number((match[3] ?? "").padEnd(2, "0"))) : null;
  });
  const total = cents.every(value => value !== null && Number.isSafeInteger(value)) ? cents.reduce<number>((sum, value) => sum + (value ?? 0), 0) : null;
  return <section aria-label="本次处理回执" style={{ borderLeft: "3px solid var(--sage)", paddingLeft: 12, margin: "12px 0" }}>
    <strong>本次处理回执 · {projection.billing_month}</strong>
    <p>{records.length ? `记录处理 ${records.length} 项。` : "记录变化以本次执行明细为准。"}
      {fees.length ? `本次补记费用 ${fees.length} 项：公司承担 ${byPayer("company")} 项，业主承担 ${byPayer("owner")} 项，其他或未明确 ${fees.length - byPayer("company") - byPayer("owner")} 项；金额见下方执行明细。` : "未在本次回执中列出新增费用；记录补齐不等于费用已补齐。"}</p>
    {!!fees.length && total !== null && Number.isSafeInteger(total) && <p>本次费用变动：¥{(total / 100).toFixed(2)}。所属日期与承担方逐笔列在执行明细中。</p>}
    <p>{projection.final_review.confirmed_settlement_count ? "已确认业主账单不会被静默改写；如本次变更影响账单，需通过结算更正重新核实。" : "业主账单是否已确认、是否已付款，以本月当前结果为准。"}</p>
    <small>这是一笔操作的完成回执，不代表整个月结已结束。</small>
  </section>;
}
