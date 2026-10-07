"use client";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { AssistantReply, MonthlyCloseProjection } from "@/lib/monthly-close";
import { monthlyCloseResultText } from "@/lib/monthly-close-result-text";
import { api, extractErrorMessage } from "@/lib/api";
import { tokens } from "@/lib/design-tokens";
import { usePrivacyMode } from "@/hooks/usePrivacyMode";
const formats = { txt: "文本 TXT", xlsx: "Excel", docx: "Word", pdf: "PDF", csv: "CSV", json: "JSON", md: "Markdown", html: "网页 HTML" };
export function QueryResultActions({ reply, projection, children, staleSnapshot = false }: { reply: AssistantReply; projection: MonthlyCloseProjection; children?: ReactNode; staleSnapshot?: boolean }) {
  const privacyMode = usePrivacyMode();
  const [notice, setNotice] = useState("");
  const [format, setFormat] = useState(reply.output?.presentation.formats[0] ?? "txt");
  const [downloadScope, setDownloadScope] = useState<string | null>(null);
  const scope = `${projection.actor_role}:${projection.billing_month}:${reply.run_id}`;
  const downloading = downloadScope === scope;
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), [privacyMode, projection.actor_role, projection.billing_month, reply.run_id]);
  if (!["read_query", "action_plan", "action_result"].includes(reply.intent) || projection.actor_role !== "admin" || reply.facts.billing_month !== projection.billing_month) return null;
  const text = monthlyCloseResultText(reply);
  const style = { minHeight: 44, border: 0, background: "transparent", color: tokens.anyu.color.stone, cursor: "pointer", font: "inherit", padding: "0 8px" };
  async function download() {
    active.current?.abort(); const controller = new AbortController(); active.current = controller;
    setDownloadScope(scope); setNotice("");
    try {
      const response = await api.get<Blob>(`/monthly-close/${projection.billing_month}/messages/${encodeURIComponent(reply.run_id)}/export`, { params: { format }, responseType: "blob", signal: controller.signal });
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(response.data);
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${projection.billing_month}-对账核对结果.${format}`; anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice(`已下载 ${formats[format]} 查询快照`);
    } catch (error) {
      if (controller.signal.aborted) return;
      const response = (error as { response?: { data?: unknown; status?: number } }).response;
      let detail = "";
      if (response?.data instanceof Blob) { try { const parsed = JSON.parse(await response.data.text()); detail = typeof parsed.detail === "string" ? parsed.detail : parsed.detail?.message ?? ""; } catch { /* Use the safe fallback. */ } }
      setNotice(detail || (response?.status === 409 ? "记录已变化，请重新查询后导出。" : extractErrorMessage(error, "下载未成功，请重试。")));
    } finally { if (active.current === controller) { active.current = null; setDownloadScope(null); } }
  }
  return <details style={{ marginTop: 8 }} open={reply.output ? true : undefined}>
    <summary style={{ ...style, width: "fit-content", lineHeight: "44px" }}>更多操作</summary>
    {children}
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
      <button type="button" disabled={privacyMode || staleSnapshot} style={style} onClick={async () => {
        try { await navigator.clipboard.writeText(text); setNotice("查询结果已复制"); }
        catch { setNotice("复制未成功，请使用下载查询结果"); }
      }}>复制查询结果</button>
      <label style={{ color: tokens.anyu.color.stone }}>文件格式 <select aria-label="查询结果文件格式" value={format} disabled={privacyMode || downloading} onChange={(event) => setFormat(event.target.value as keyof typeof formats)} style={{ ...style, border: `1px solid ${tokens.anyu.color.linen}`, borderRadius: 8 }}>{Object.entries(formats).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <button type="button" disabled={privacyMode || staleSnapshot || downloading} style={style} onClick={download}>{downloading ? "正在准备文件…" : "下载查询结果"}</button>
    </div>
    {reply.output && reply.output.presentation.formats.length > 1 && <div style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>你需要的格式：{reply.output.presentation.formats.map((item) => formats[item]).join("、")}。可逐一选择下载。</div>}
    {privacyMode && <div style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>隐私演示模式禁止复制或下载查询快照。</div>}
    {staleSnapshot && <div style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>记录已变化。请在对话中重新查询，再复制或导出新结果。</div>}
    {reply.narration_degraded && !["cost_responsibility", "amount_summary"].includes(String(reply.facts.query_mode)) && <div style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>已直接查询系统记录。</div>}
    {notice && <div role="status" style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>{notice}</div>}
  </details>;
}
