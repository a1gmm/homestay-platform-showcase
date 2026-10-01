"use client";

import { Alert, Button, Input, Spin } from "antd";
import Link from "next/link";
import { useState } from "react";
import dayjs from "dayjs";
import { useBypmsOrderDiagnosis } from "@/hooks/useBypmsSyncAdmin";
import { tokens } from "@/lib/design-tokens";

const labels = {
  not_found: "当前暂存范围未找到", missing_order: "已抓取，尚未关联系统订单",
  linked: "已关联系统订单", manual_review: "需要人工核对", cancelled: "上游记录已取消", unavailable: "诊断暂不可用",
};

export function OrderSyncDiagnosis() {
  const [input, setInput] = useState("");
  const [submitted, setSubmitted] = useState("");
  const query = useBypmsOrderDiagnosis(submitted);
  const result = query.data;
  const valid = /^[A-Za-z0-9_-]{1,100}$/.test(input.trim());
  return <section aria-label="订单同步诊断" style={{ marginTop: 16, padding: 20, border: `1px solid ${tokens.color.bg.border}`, borderRadius: 12 }}>
    <h2 style={{ marginTop: 0 }}>查一笔订单的同步情况</h2>
    <form onSubmit={(event) => { event.preventDefault(); if (valid) { if (submitted === input.trim()) void query.refetch(); else setSubmitted(input.trim()); } }} style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
      <Input aria-label="平台订单号" value={input} maxLength={100} onChange={(event) => setInput(event.target.value)} placeholder="输入准确的平台订单号" style={{ minHeight: 44, fontSize: 16, flex: "1 1 240px" }} />
      <Button htmlType="submit" disabled={!valid} style={{ minHeight: 44 }}>查询同步情况</Button>
    </form>
    <p style={{ color: tokens.color.text.secondary }}>查询只读取记录。候选订单需要人工核实，完整重试不会解除人工锁定或跳过冲突检查。</p>
    {query.isFetching && <Spin size="small" />}
    {query.isError && <Alert type="error" showIcon message="诊断读取失败，请重试；当前无法确认订单状态。" />}
    {result && !query.isError && <div aria-live="polite">
      <h3>{labels[result.state]}</h3>
      <p>最近抓取：{result.fetched_at ? dayjs(result.fetched_at).format("YYYY-MM-DD HH:mm:ss") : "没有记录"} · {result.staging_stale ? "尚无新鲜记录" : "记录在时效内"}</p>
      {result.reasons.length > 0 && <ul>{result.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}
      <p>{result.next_action}</p>
      {result.order_ids.map((id) => <p key={id}><Link href={`/orders?keyword=${encodeURIComponent(id)}`}>打开已关联订单 {id}</Link></p>)}
      {result.candidate_order_ids.map((id) => <p key={id}><Link href={`/orders?keyword=${encodeURIComponent(id)}`}>核对候选订单 {id}（尚未确认关联）</Link></p>)}
      {result.state === "missing_order" && <Link href="/finance/reconciliation">进入订单对账核对</Link>}
    </div>}
  </section>;
}
