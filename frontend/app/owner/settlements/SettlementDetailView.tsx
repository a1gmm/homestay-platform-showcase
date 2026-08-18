"use client";

// 结算详情展示组件群。从 page.tsx 抽出——Next.js App Router 的 page.tsx
// 只允许默认导出页面，具名导出组件会让 `next build` 的页面类型校验失败
// （TS/vitest 不报，只有 next build 报）。展示件集中放这里，page.tsx 只做
// 数据获取 + 布局。SettlementDetailView 同时供组件测试 import。

import { useEffect, useState } from "react";
import { Tooltip } from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import type { SettlementBrief, SettlementDetail } from "@/lib/owner-api";
import { SETTLEMENT_TERM_TIPS, formatShareRatio } from "@/lib/owner-settlement-terms";
import { aggregateBreakdown, aggregateRoomBreakdown, round2, type ExpenseLineItem } from "@/lib/owner-expense-breakdown";
import { fmtMille, MILLE_NOTE } from "@/lib/utils";

export const STATUS_LABEL: Record<string, { label: string; color: string }> = {
  pending: { label: "待确认", color: "orange" },
  confirmed: { label: "已确认", color: "blue" },
  paid: { label: "已打款", color: "green" },
  disputed: { label: "有争议", color: "red" },
};

export const fmt = (n: string | number | null | undefined) => {
  // null/undefined = 隐藏金额账号后端置空 → 「——」（真实 0 仍显示 0.00）。
  if (n === null || n === undefined) return "——";
  const v = typeof n === "string" ? parseFloat(n) : n;
  if (Number.isNaN(v)) return "——";
  return v.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

// 到厘展示 + 小字统一走 lib/utils（管理端/业主端共用一处口径）。
// 本文件转出，便于 page.tsx 与组件测试从同一处 import。
export { fmtMille, MILLE_NOTE };

// 术语标签 + ⓘ 解释。trigger 带 click:手机上没有 hover,业主一定是点出来的;
// padding 撑大可点区域(移动端军规,纯图标 11px 根本点不中)。
function Term({ label, tipKey, light }: { label: string; tipKey: string; light?: boolean }) {
  return (
    <Tooltip title={SETTLEMENT_TERM_TIPS[tipKey]} trigger={["hover", "click"]}>
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          cursor: "help",
          padding: "6px 6px 6px 0",
          margin: "-6px -6px -6px 0",
        }}
      >
        {label}
        <QuestionCircleOutlined style={{ fontSize: 11, color: light ? "#A89680" : "#B3A78F" }} />
      </span>
    </Tooltip>
  );
}

// 单行支出：标签 + 「¥单价 × 数量」+ 金额。汇总卡与逐房展开共用。
function ExpenseLine({ t }: { t: ExpenseLineItem }) {
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: 8, fontSize: 12, lineHeight: 2 }}>
      <span style={{ color: "#7A6F5F", whiteSpace: "nowrap" }}>{t.label}</span>
      {t.detail && (
        <span style={{ color: "#A89680", fontSize: 11, whiteSpace: "nowrap" }}>{t.detail}</span>
      )}
      <span style={{ flex: 1, borderBottom: "0.5px dotted #E5DDCB", minWidth: 8 }} />
      <span className="serif" style={{ color: "#2B2721", letterSpacing: 0, whiteSpace: "nowrap" }}>
        ¥{fmt(t.ownerAmount)}
      </span>
    </div>
  );
}

// 跨房支出汇总卡。totals 为空 → 不渲染。
// fail-closed：合计必须等于概览卡的「扣除支出」。两个数同屏相距 100px，
// 一旦对不上，业主看到的就是自相矛盾——宁可不显示明细，也不显示矛盾。
// 这条不变量「验不了」（expected 非数字/空）时也算失败，一并不渲染——
// fail-closed 的含义是「不能证明一致就不显示」，不是「只在证明了不一致才不显示」。
// 今天的数据永远不会触发（生产实测一致）；这是防将来新增行形态被静默丢弃。
function ExpenseSummary({ totals, expected }: { totals: ExpenseLineItem[]; expected: string }) {
  if (totals.length === 0) return null;
  const total = round2(totals.reduce((s, t) => s + t.ownerAmount, 0));
  const expectedNum = parseFloat(expected);
  if (!Number.isFinite(expectedNum) || Math.abs(total - expectedNum) > 0.01) return null;
  return (
    <div style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 12, padding: 14, marginBottom: 16 }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: "#2B2721", marginBottom: 8 }}>本月支出明细</div>
      {totals.map((t) => (
        <ExpenseLine key={t.key} t={t} />
      ))}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginTop: 8,
          paddingTop: 8,
          borderTop: "0.5px solid #E5DDCB",
          fontSize: 13,
        }}
      >
        <span style={{ color: "#5C5547", fontWeight: 600 }}>合计</span>
        <span className="serif" style={{ color: "#2B2721", fontWeight: 600, letterSpacing: 0 }}>
          ¥{fmt(total)}
        </span>
      </div>
    </div>
  );
}

// 结算详情视图：概览深色卡（实际打款 + 净收入/扣除）+ 按房间明细。移动端 Drawer 与桌面右栏共用。
// 结算状态（待确认/已确认/已打款）是内部流转口径，业主端不展示，概览固定两列。
export function SettlementDetailView({ detail }: { detail: SettlementDetail }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  useEffect(() => {
    setExpanded(new Set());
  }, [detail.settlement_id]);
  const rates = detail.service_fee_rates;
  const toggle = (roomId: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(roomId) ? next.delete(roomId) : next.add(roomId);
      return next;
    });
  return (
    <div>
      <div data-testid="settlement-overview" style={{ background: "#2B2721", borderRadius: 16, padding: 20, color: "#FBF8F1", marginBottom: 16 }}>
        <div style={{ fontSize: 12, color: "#A89680" }}>
          <Term label="实际打款金额" tipKey="actual_owner_amount" light /> · {detail.billing_month}
        </div>
        <div className="serif" style={{ fontSize: 32, fontWeight: 400, marginTop: 6, letterSpacing: 0 }}>
          ¥{fmtMille(detail.actual_owner_amount_precise, detail.actual_owner_amount)}
        </div>
        {/* 口径前置：让业主看到扣除是公式的一部分，不是事后刨的。
            刻意不写「净收入 × 70%」——系统是逐房算分成再求和，总额乘一次会差
            1 分（张总 2026-06 实测 16579.39 vs 16579.38）。印一个业主一按计算器
            就对不上的算式，是自己制造信任事故。这两步是精确的。 */}
        <div style={{ marginTop: 12, fontSize: 11, color: "#A89680", lineHeight: 1.9 }}>
          <div>分成 ¥{fmtMille(detail.owner_amount_precise, detail.owner_amount)}</div>
          <div>− 业主承担支出 ¥{fmt(detail.deducted_expenses)}</div>
          <div style={{ color: "#FBF8F1" }}>= 实际打款 ¥{fmtMille(detail.actual_owner_amount_precise, detail.actual_owner_amount)}</div>
          <div style={{ marginTop: 6, color: "#8A7B63" }}>{MILLE_NOTE}</div>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 12,
            marginTop: 16,
            paddingTop: 16,
            borderTop: "0.5px solid #5C5547",
          }}
        >
          <div>
            <div style={{ fontSize: 11, color: "#A89680" }}>
              <Term label="总净收入" tipKey="total_net_revenue" light />
            </div>
            <div className="serif" style={{ fontSize: 16, fontWeight: 400, marginTop: 4, letterSpacing: 0 }}>
              ¥{fmt(detail.total_net_revenue)}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: "#A89680" }}>
              <Term label="扣除支出" tipKey="deducted_expenses" light />
            </div>
            <div className="serif" style={{ fontSize: 16, fontWeight: 400, marginTop: 4, letterSpacing: 0 }}>
              ¥{fmt(detail.deducted_expenses)}
            </div>
          </div>
        </div>
      </div>

      <ExpenseSummary totals={aggregateBreakdown(detail.items, rates)} expected={detail.deducted_expenses} />

      <div style={{ fontSize: 15, fontWeight: 600, color: "#2B2721", marginBottom: 10 }}>
        按房间明细 ({detail.items.length})
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 8 }}>
        {detail.items.map((it) => {
          const ratioText = formatShareRatio(it.share_ratio_snapshot);
          const roomBreakdown = aggregateRoomBreakdown(it, rates);
          const canExpand = roomBreakdown.length > 0;
          const isOpen = expanded.has(it.item_id);
          return (
            <div key={it.item_id} style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 10, padding: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
                <div className="serif" style={{ fontSize: 15, fontWeight: 400, letterSpacing: 0 }}>
                  {it.room_name || it.label || it.room_id}
                </div>
                <Tooltip title={SETTLEMENT_TERM_TIPS.room_owner_net} trigger={["hover", "click"]}>
                  <div className="serif" style={{ color: "#2B2721", fontWeight: 400, letterSpacing: 0, cursor: "help" }}>
                    ¥{fmtMille(it.owner_net_amount_precise, it.owner_net_amount)}
                  </div>
                </Tooltip>
              </div>
              <div style={{ fontSize: 11, color: "#7A6F5F", lineHeight: 1.6 }}>
                {it.order_count} 单 · 收入 ¥{fmt(it.revenue)} · 佣金 ¥{fmt(it.commission)} ·{" "}
                {canExpand ? (
                  <span
                    role="button"
                    tabIndex={0}
                    onClick={() => toggle(it.item_id)}
                    onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(it.item_id); } }}
                    style={{ cursor: "pointer", color: "#2B2721", padding: "6px 0", margin: "-6px 0" }}
                  >
                    业主支出 ¥{fmt(it.owner_expenses)} {isOpen ? "▾" : "▸"}
                  </span>
                ) : (
                  <>业主支出 ¥{fmt(it.owner_expenses)}</>
                )}
                {ratioText && (
                  <Tooltip title={SETTLEMENT_TERM_TIPS.share_ratio} trigger={["hover", "click"]}>
                    <span style={{ cursor: "help" }}> · 分成 {ratioText}</span>
                  </Tooltip>
                )}
              </div>
              {canExpand && isOpen && (
                <div style={{ marginTop: 8, paddingTop: 8, borderTop: "0.5px solid #E5DDCB" }}>
                  {roomBreakdown.map((t) => (
                    <ExpenseLine key={t.key} t={t} />
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function SettlementCard({ s, onClick, selected }: { s: SettlementBrief; onClick: () => void; selected?: boolean }) {
  return (
    <div
      onClick={onClick}
      className="card-hoverable"
      style={{
        background: "#F5F1EA",
        border: selected ? "0.5px solid #2B2721" : "0.5px solid #E5DDCB",
        borderRadius: 12,
        padding: 14,
        marginBottom: 10,
        cursor: "pointer",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <div className="serif" style={{ fontSize: 17, fontWeight: 400, letterSpacing: 0 }}>
            {s.billing_month}
          </div>
          <div style={{ fontSize: 11, color: "#7A6F5F", marginTop: 4 }}>
            生成于 {dayjs(s.created_at).format("YYYY-MM-DD")}
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div className="serif" style={{ fontSize: 18, fontWeight: 400, color: "#2B2721", letterSpacing: 0 }}>
            ¥{fmtMille(s.actual_owner_amount_precise, s.actual_owner_amount)}
          </div>
        </div>
      </div>
    </div>
  );
}
