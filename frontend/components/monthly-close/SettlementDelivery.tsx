"use client";
import { useEffect, useRef, useState } from "react";
import { monthlyCloseApi } from "@/lib/api";
import { getPersistedAuthSessionId, useAuthStore } from "@/lib/auth";
import { downloadBlob } from "@/lib/utils";
import type { MonthlyCloseProjection } from "@/lib/monthly-close";
import { usePrivacyMode } from "@/hooks/usePrivacyMode";

export function SettlementDelivery({ projection }: { projection: MonthlyCloseProjection }) {
  const privacyMode = usePrivacyMode();
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    const session = getPersistedAuthSessionId();
    const actor = useAuthStore.getState();
    const unsubscribe = useAuthStore.subscribe(next => {
      if (session !== getPersistedAuthSessionId() || actor.user?.user_id !== next.user?.user_id || actor.user?.role !== next.user?.role) request.current?.abort();
    });
    const storage = () => { if (session !== getPersistedAuthSessionId()) request.current?.abort(); };
    window.addEventListener("storage", storage);
    return () => { request.current?.abort(); unsubscribe(); window.removeEventListener("storage", storage); };
  }, [projection.billing_month, projection.actor_role]);
  useEffect(() => { if (privacyMode) request.current?.abort(); }, [privacyMode]);
  if (projection.actor_role !== "admin") return null;
  const rows = projection.final_review.settlements?.filter(row => row.billing_month === projection.billing_month) ?? [];
  const download = async (id: string, name: string, kind: "package" | "income-detail") => {
    if (privacyMode || request.current || !/^[A-Za-z0-9_-]+$/.test(id)) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(`${id}:${kind}`); setNotice(null);
    try {
      const response = await monthlyCloseApi.downloadTaskArtifact(`/export/settlements/${id}/${kind}`, controller.signal);
      if (controller.signal.aborted) return;
      downloadBlob(response.data, `${projection.billing_month}-${name}-${kind === "package" ? "业主结算单与明细" : "订单收入明细"}.xlsx`.replace(/[\\/:*?"<>|]/g, "_"));
      setNotice("已下载。文件依据已保存的结算快照生成；付款状态请按实际打款登记。");
    } catch { if (!controller.signal.aborted) setNotice("下载未完成，请重试；账单未被更改。"); }
    finally { request.current = null; if (!controller.signal.aborted) setBusy(null); }
  };
  return <section aria-label="本月账单交付" style={{ borderTop: "1px solid var(--linen)", paddingTop: 16 }}>
    <h3 style={{ fontSize: 18, marginTop: 0 }}>账单与交付文件</h3>
    {rows.map((row) => <div key={row.settlement_id} style={{ borderBottom: "1px solid var(--linen)", padding: "12px 0" }}>
      <strong>{row.owner_name}</strong> · 应付 ¥{row.amount}
      <p>{({ confirmed: "账单已确认 · 付款尚未登记", paid: "账单已确认 · 已登记付款", pending: "待复核确认，暂不作为定稿交付", disputed: "账单有争议，需处理后再定稿" } as Record<string, string>)[row.status] ?? "状态待核实"}</p>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        <button disabled={privacyMode || !!busy} style={{ minHeight: 44, padding: "0 16px", border: "1px solid var(--linen)", borderRadius: 999, background: "var(--shell)", color: "var(--ink)", font: "inherit" }} onClick={() => void download(row.settlement_id, row.owner_name, "package")}>{busy === `${row.settlement_id}:package` ? "正在下载…" : "下载结算单与明细"}</button>
        <button disabled={privacyMode || !!busy} style={{ minHeight: 44, padding: "0 16px", border: "1px solid var(--linen)", borderRadius: 999, background: "var(--shell)", color: "var(--ink)", font: "inherit" }} onClick={() => void download(row.settlement_id, row.owner_name, "income-detail")}>下载订单收入明细</button>
      </div>
    </div>)}
    {!rows.length && <p>账单明细可从下方结算列表查看。尚未生成的账单需先核对记录与费用。</p>}
    <p>公司经营报表：{projection.final_review.state === "verified" ? "本月已关账，可查看归档报告。" : "尚未关账，当前经营报表仍需结合待核实事项复核。"}</p>
    {notice && <p role="status">{notice}</p>}
  </section>;
}
