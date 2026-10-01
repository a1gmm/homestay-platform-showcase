"use client";

import { useState, type ReactNode } from "react";
import type { AssistantReply, MonthlyCloseProjection } from "@/lib/monthly-close";
import { monthlyCloseResultText } from "@/lib/monthly-close-result-text";
import { tokens } from "@/lib/design-tokens";
import { usePrivacyMode } from "@/hooks/usePrivacyMode";

export function QueryResultActions({ reply, projection, children }: { reply: AssistantReply; projection: MonthlyCloseProjection; children?: ReactNode }) {
  const privacyMode = usePrivacyMode();
  const [notice, setNotice] = useState("");
  if (reply.tool !== "review_month" || ["cost_responsibility", "amount_summary"].includes(String(reply.facts.query_mode)) || projection.actor_role !== "admin" || reply.facts.billing_month !== projection.billing_month) return null;
  const text = monthlyCloseResultText(reply);
  const style = { minHeight: 44, border: 0, background: "transparent", color: tokens.anyu.color.stone, cursor: "pointer", font: "inherit", padding: "0 8px" };
  return <details style={{ marginTop: 8 }}>
    <summary style={{ ...style, width: "fit-content", lineHeight: "44px" }}>更多操作</summary>
    {children}
    <button type="button" disabled={privacyMode} style={style} onClick={async () => {
      try { await navigator.clipboard.writeText(text); setNotice("查询结果已复制"); }
      catch { setNotice("复制未成功，请使用下载查询结果"); }
    }}>复制查询结果</button>
    <button type="button" disabled={privacyMode} style={style} onClick={() => {
      try {
        const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = `${projection.billing_month}-月结查询结果.txt`;
        anchor.click();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        setNotice("已下载本次查询快照");
      } catch { setNotice("下载未成功，请重试"); }
    }}>下载查询结果</button>
    {privacyMode && <div style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>隐私演示模式禁止复制或下载查询快照。</div>}
    {reply.narration_degraded && <div style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>已直接查询系统记录。</div>}
    {notice && <div role="status" style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>{notice}</div>}
  </details>;
}
