"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, extractErrorMessage } from "@/lib/api";
import type { AssistantReply, MonthlyCloseIssue, MonthlyCloseProjection } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";
import { QueryResultActions } from "./QueryResultActions";

type DetailItem = MonthlyCloseIssue & { ordinal: number; document_id?: string };
type DetailPage = {
  billing_month: string; step_key: string; evidence_hash: string;
  result_state: "current"; total: number; offset: number; limit: number;
  has_more: boolean; items: DetailItem[];
};
const buttonStyle = { minHeight: 44, borderRadius: 999, padding: "0 16px", border: `1px solid ${tokens.anyu.color.linen}`, background: tokens.anyu.color.shell, color: tokens.anyu.color.ink.default, font: "inherit", cursor: "pointer" } as const;
const quietStyle = { minHeight: 44, padding: "0 4px", border: 0, background: "transparent", color: tokens.anyu.color.stone, font: "inherit", cursor: "pointer" } as const;
const handlingPaths = new Set(["/orders", "/finance", "/finance/billing-recon", "/finance/utility-recon", "/settlements"]);
function handlingHref(issue: MonthlyCloseIssue, month: string) {
  const action = issue.action;
  if (action?.kind !== "navigate" || !handlingPaths.has(action.path)) return null;
  const params = new URLSearchParams(action.query || {});
  params.set("returnTo", `/finance/monthly-close?month=${month}`);
  return `${action.path}?${params.toString()}`;
}

export function hasStructuredProgress(reply: AssistantReply, projection: MonthlyCloseProjection) {
  return reply.tool === "review_month" && projection.actor_role === "admin"
    && reply.facts.billing_month === projection.billing_month
    && typeof reply.facts.detail_total === "number"
    && !reply.output
    && (["cleaning_statement", "linen_statement", "utility_expense", "ota_statement", "operating_expenses"].includes(String(reply.facts.focus)) || projection.workflow_evidence.some((step) => step.step_key === reply.facts.focus));
}

export function ProgressDetails({ reply, projection, onSelectDocument, onOpenOrder, onOpenWorkflow }: {
  reply: AssistantReply; projection: MonthlyCloseProjection;
  onSelectDocument?: (documentId: string) => void;
  onOpenOrder?: (orderId: string) => void;
  onOpenWorkflow?: (focus: string) => void;
}) {
  const [page, setPage] = useState<DetailPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const activeRequest = useRef<AbortController | null>(null);
  useEffect(() => () => activeRequest.current?.abort(), []);
  const focus = typeof reply.facts.focus === "string" ? reply.facts.focus : "";
  if (!hasStructuredProgress(reply, projection)) return null;
  const snapshotTotal = reply.facts.detail_total as number;
  const snapshotHash = typeof reply.facts.detail_evidence_hash === "string" ? reply.facts.detail_evidence_hash : undefined;
  const selectedOrdinal = typeof reply.facts.selected_ordinal === "number" ? reply.facts.selected_ordinal : undefined;
  const previews: DetailItem[] = Array.isArray(reply.facts.detail_items) ? reply.facts.detail_items.map((row, index) => ({
    ...row, ordinal: row.ordinal ?? index + 1, resource_id: row.order_id ?? String(index), code: row.issue_code ?? "", message: row.cause ?? "",
  })) : [];
  const items = error ? [] : page?.items ?? previews;
  const total = page?.total ?? snapshotTotal;
  const step = Array.isArray(reply.facts.steps) ? reply.facts.steps.find((row) => row.step_key === focus) : undefined;
  const label = step?.label ?? ({ cleaning_statement: "保洁打扫记录", linen_statement: "布草洗涤", utility_expense: "水电支出", ota_statement: "OTA 平台账单", operating_expenses: "运营支出" }[focus] ?? "这一步");
  const documentIds = Array.from(new Set(previews.map(item => item.document_id).filter(Boolean)));
  const visibleDocuments = new Set(projection.sources.flatMap((source) => source.documents.map((document) => document.document_id)));
  const canHandle = (item: DetailItem) => (typeof item.order_id === "string" && onOpenOrder) || handlingHref(item, projection.billing_month)
    || (item.document_id && visibleDocuments.has(item.document_id) && onSelectDocument);

  async function load(offset: number, refresh = false) {
    activeRequest.current?.abort();
    const controller = new AbortController(); activeRequest.current = controller;
    setLoading(true); setError("");
    try {
      const response = await api.get<DetailPage>(`/monthly-close/${projection.billing_month}/progress-details`, {
        params: { step_key: focus, document_id: documentIds.length === 1 ? documentIds[0] : undefined, offset: selectedOrdinal ? selectedOrdinal - 1 : offset, limit: selectedOrdinal ? 1 : 20, expected_evidence_hash: refresh && !selectedOrdinal ? undefined : page?.evidence_hash ?? snapshotHash }, signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (response.data.billing_month !== projection.billing_month || response.data.step_key !== focus) throw new Error("返回月份不一致，请重试。");
      setPage(response.data);
    } catch (caught) {
      if (controller.signal.aborted) return;
      const stale = (caught as { response?: { status?: number } }).response?.status === 409;
      setPage(null);
      setError(stale ? (selectedOrdinal ? "记录已变化，旧序号不能对应新记录。请在对话中重新查询后选择。" : "记录已变化，请更新后再处理。") : extractErrorMessage(caught, "读取失败，请重试。"));
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }

  return <section aria-label="完整月结事项" style={{ display: "grid", gap: 12, overflowWrap: "anywhere" }}>
    {typeof reply.facts.selected_ordinal === "number" && <div>你选中的是第 {reply.facts.selected_ordinal} 项。</div>}
    <div>{error ? "需要重新核对当前记录。" : total ? `${label}还有 ${total} 项待处理。` : `${label}本次检查没有发现待处理事项。`}</div>
    {error && <div role="alert">{error}</div>}
    <ol start={(page?.offset ?? 0) + 1} style={{ margin: 0, paddingLeft: total === 1 ? 0 : 22, listStyle: total === 1 ? "none" : undefined, display: "grid", gap: 20 }}>
      {items.map((item) => {
        const href = handlingHref(item, projection.billing_month);
        const orderId = typeof item.order_id === "string" ? item.order_id : undefined;
        const missingIdentity = (item.code || item.issue_code) === "missing_platform_order_id";
        const failures = Array.isArray(item.failed) ? item.failed.filter((row): row is { row: number; errors: string } => Boolean(row) && typeof row === "object" && typeof (row as { errors?: unknown }).errors === "string") : [];
        return <li key={item.ordinal} style={{ display: "grid", gap: 8 }}>
          <div style={{ fontWeight: 500 }}>{item.subject ?? "待处理事项"}</div>
          <div style={{ whiteSpace: "pre-wrap" }}>{item.cause ?? item.message}</div>
          {reply.facts.query_mode === "details" && <div style={{ whiteSpace: "pre-wrap" }}>处理建议：{item.next_step}{typeof item.source === "string" && <div style={{ color: tokens.anyu.color.stone }}>依据：{item.source}</div>}</div>}
          {missingIdentity && <div>找到真实的平台订单号后，直接发给我；我会先给你确认，再保存。</div>}
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
            {orderId && onOpenOrder ? <button type="button" style={buttonStyle} onClick={() => onOpenOrder(orderId)}>查看订单</button>
              : href ? <Link href={href} className="link-underline" style={{ ...quietStyle, display: "inline-flex", alignItems: "center" }}>{item.action?.label}</Link>
                : item.document_id && visibleDocuments.has(item.document_id) && onSelectDocument ? <button type="button" style={buttonStyle} onClick={() => onSelectDocument(item.document_id!)}>查看对应文件</button> : null}
          </div>
          <details onToggle={(event) => { if (event.currentTarget.open && !page && !loading && reply.facts.query_mode !== "details") void load(0); }}>
            <summary style={{ ...quietStyle, display: "list-item", width: "fit-content", lineHeight: "44px" }}>处理依据</summary>
            <div style={{ color: tokens.anyu.color.stone, whiteSpace: "pre-wrap", lineHeight: 1.8 }}>
              {!page && reply.facts.query_mode !== "details" ? <span role="status">{loading ? "正在读取完整依据…" : "展开后读取完整依据"}</span> : <>
                {item.impact && <div>{item.impact}</div>}
                <div>{item.next_step}</div>
                {failures.map((row, index) => <div key={index}>第 {row.row} 行：{row.errors}</div>)}
              </>}
            </div>
          </details>
        </li>;
      })}
    </ol>
    {!selectedOrdinal && !error && !page && total > previews.length && <button type="button" style={quietStyle} disabled={loading} onClick={() => load(0)}>{loading ? "正在读取…" : `查看全部 ${total} 项`}</button>}
    {!error && onOpenWorkflow && !items.some(canHandle) && <button type="button" style={{ ...buttonStyle, justifySelf: "start" }} onClick={() => onOpenWorkflow(focus)}>继续处理这一步</button>}
    {!selectedOrdinal && page && page.total > page.limit && <nav aria-label="完整事项分页" style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
      <button type="button" style={quietStyle} disabled={loading || page.offset === 0} onClick={() => load(Math.max(0, page.offset - page.limit))}>上一页</button>
      <span style={{ color: tokens.anyu.color.stone }}>{Math.floor(page.offset / page.limit) + 1} / {Math.ceil(page.total / page.limit)}</span>
      <button type="button" style={quietStyle} disabled={loading || !page.has_more} onClick={() => load(page.offset + page.limit)}>下一页</button>
    </nav>}
    {error && <button type="button" style={{ ...quietStyle, justifySelf: "start" }} disabled={loading} onClick={() => load(0, true)}>{loading ? "更新中…" : "重新读取"}</button>}
    <QueryResultActions reply={reply} projection={projection} staleSnapshot={Boolean(error || (page && page.evidence_hash !== snapshotHash))}>
      {!error && <div style={{ display: "flex", alignItems: "center", gap: 12, color: tokens.anyu.color.stone, fontSize: 12 }}>
        <span>{page ? "已更新查询结果" : "本次查询结果"}</span>
        <button type="button" style={{ ...quietStyle, fontSize: 12 }} disabled={loading} onClick={() => load(0, true)}>{loading ? "更新中…" : "更新结果"}</button>
      </div>}
    </QueryResultActions>
  </section>;
}
