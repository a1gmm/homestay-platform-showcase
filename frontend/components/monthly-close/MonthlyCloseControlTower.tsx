import { Button, Empty, Tag } from "antd";

import type { MonthlyCloseOverview } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";

function monthLabel(month: string) {
  const [year, value] = month.split("-");
  return `${year}年${value}月`;
}

function activityLabel(updatedAt: string | null, actor: string | null) {
  const timestamp = updatedAt ? updatedAt.replace("T", " ").slice(0, 16) : "暂无时间";
  return `最后变化 ${timestamp} · ${actor || "系统"}`;
}

export function MonthlyCloseControlTower({ rows, selectedMonth, onSelect }: { rows: MonthlyCloseOverview[]; selectedMonth: string; onSelect: (month: string) => void }) {
  return (
    <section className="monthly-close-overview" aria-label="所有月份月结总览" style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, overflow: "hidden" }}>
      <div className="monthly-close-overview-header" style={{ padding: "15px 18px", borderBottom: `0.5px solid ${tokens.anyu.color.linen}` }}>
        <div className="serif" style={{ fontSize: 20 }}>所有月份</div>
        <div style={{ color: tokens.color.text.secondary, marginTop: 4 }}>一眼看到哪个月卡住、缺什么，直接继续处理。</div>
      </div>
      {rows.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有月结记录" /> : rows.map((row) => {
        const selected = row.billing_month === selectedMonth;
        const complete = row.status === "completed";
        const label = monthLabel(row.billing_month);
        return (
          <div className="monthly-close-overview-row" key={row.cycle_id} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 14, padding: "13px 18px", background: selected ? tokens.anyu.color.shell : "transparent", borderBottom: `0.5px solid ${tokens.anyu.color.linen}` }}>
            <div className="monthly-close-overview-month" style={{ flex: "1 1 140px", minWidth: 0, overflowWrap: "anywhere" }}><div className="serif" style={{ fontSize: 17 }}>{label}</div><div style={{ color: tokens.color.text.tertiary, fontSize: 12, marginTop: 2 }}>已完成 {row.progress} / 9 步</div><div style={{ color: tokens.color.text.tertiary, fontSize: 12, marginTop: 2 }}>{activityLabel(row.updated_at, row.last_actor)}</div></div>
            <div className="monthly-close-overview-status" style={{ flex: "1 1 140px", minWidth: 0, overflowWrap: "anywhere" }}><div>{complete ? "月结已完成" : row.current_step_label || "等待开始"}</div><div style={{ color: tokens.color.text.tertiary, fontSize: 12, marginTop: 2 }}>{complete ? "可查看结算历史" : "当前应处理步骤"}</div></div>
            <div className="monthly-close-overview-signals" style={{ display: "flex", gap: 7, flexWrap: "wrap", flex: "1 1 180px", minWidth: 0 }}>
              {row.missing_source_count > 0 && <Tag color="warning">缺 {row.missing_source_count} 份资料</Tag>}
              {row.blocking_count > 0 && <Tag color="error">{row.blocking_count} 个阻断项</Tag>}
              {row.inbox_pending_count > 0 && <Tag color="processing">{row.inbox_pending_count} 个收件待确认</Tag>}
              {complete && <Tag color="success">已完成</Tag>}
              {!complete && row.missing_source_count === 0 && row.blocking_count === 0 && row.inbox_pending_count === 0 && <Tag color="success">可继续</Tag>}
            </div>
            <Button style={{ flexShrink: 0, minHeight: 44 }} className="monthly-close-overview-action" aria-label={`继续 ${label}月结`} onClick={() => onSelect(row.billing_month)}>{complete ? "查看" : "继续"} →</Button>
          </div>
        );
      })}
    </section>
  );
}
