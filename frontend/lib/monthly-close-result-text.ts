import type { AssistantReply } from "./monthly-close";

/** Export the saved query facts, never re-calculate money or treat a snapshot as live. */
export function monthlyCloseResultText(reply: AssistantReply): string {
  const facts = reply.facts;
  const lines = [`${facts.billing_month} 月结查询结果`, "本次查询快照；不是结算凭证。记录变化后请重新查询。", "", reply.message];
  if (reply.tool === "review_month") {
    if (typeof facts.detail_total === "number") {
      const shown = Array.isArray(facts.detail_items) ? facts.detail_items.length : 0;
      lines.push("", `当时共 ${facts.detail_total} 项；本快照仅包含 ${shown} 项摘要。请在聊天中打开“查看全部事项与完整依据”读取完整清单。`);
    }
    for (const row of (facts.detail_items ?? []) as Array<{subject: string; cause: string; next_step: string}>) {
      lines.push("", `${row.subject}：${row.cause}`, `处理建议：${row.next_step}`);
    }
    lines.push("", "资料与下一步");
    for (const row of (facts.sources ?? []) as Array<{label: string; detail: string; next_step: string}>) {
      lines.push(`- ${row.label}：${row.detail}；${row.next_step}`);
    }
    lines.push("", "月结步骤");
    for (const row of (facts.steps ?? []) as Array<{label: string; status: string; blocking_count: number}>) {
      lines.push(`- ${row.label}：${row.status === "confirmed" ? "已确认" : row.blocking_count ? `${row.blocking_count} 项待处理` : "尚待确认"}`);
    }
  }
  lines.push("", `查询编号：${reply.run_id}`);
  return lines.join("\n");
}
