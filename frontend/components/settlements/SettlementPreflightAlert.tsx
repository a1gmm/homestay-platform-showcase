import { useState } from "react";
import { Alert, Input, List, Spin, Typography } from "antd";

import type { SettlementPreflightIssue, SettlementPreflightReport } from "@/lib/types";


const { Text } = Typography;

const ISSUE_LABELS: Record<string, string> = {
  nonplatform_commission: "线下单仍在按平台金额结算",
  invalid_order_expense: "取消/删除订单费用",
  owner_self_owner_expense: "自住费用承担方错误",
  open_reconciliation: "平台账单差异",
  duplicate_platform_order: "重复平台订单号",
};

const ISSUE_MESSAGES: Record<string, string> = {
  nonplatform_commission:
    "这张订单已改为线下收款，但旧的平台佣金或补贴仍在参与结算。请回到订单确认渠道和实收金额后重新保存，系统会立即清除旧的平台计价。",
};

function issueReference(issue: SettlementPreflightIssue): string {
  const amountLabel = issue.code === "nonplatform_commission"
    ? "可能影响结算"
    : "金额";
  const identity = [issue.guest_name, issue.room_name && `${issue.room_name}房`,
    issue.check_in && issue.check_out && `${issue.check_in} 至 ${issue.check_out}`].filter(Boolean).join(" · ");
  if (identity) return [identity, issue.amount != null && `${amountLabel} ¥${issue.amount}`].filter(Boolean).join(" · ");
  return [
    issue.order_id && `订单 ${issue.order_id}`,
    issue.room_id && `房间 ${issue.room_id}`,
    issue.expense_id && `费用 ${issue.expense_id}`,
    issue.recon_diff_id && `差异 ${issue.recon_diff_id}`,
    issue.platform_order_id && `平台单 ${issue.platform_order_id}`,
    issue.amount && `${amountLabel} ¥${issue.amount}`,
  ].filter(Boolean).join(" · ");
}

export default function SettlementPreflightAlert({
  report,
  loading,
}: {
  report?: SettlementPreflightReport;
  loading: boolean;
}) {
  const [search, setSearch] = useState("");
  if (loading) {
    return <Alert type="info" showIcon message={<><Spin size="small" /> 正在执行月结体检…</>} />;
  }
  if (!report) return null;
  const explanations = report.explanations ?? [];
  const missing = explanations.filter((item) => item.code === "service_fee_missing");
  const explained = explanations.filter((item) => item.code !== "service_fee_missing");
  const filtered = explained.filter((item) => `${item.guest_name ?? ""} ${item.room_name ?? ""} ${item.message}`.includes(search.trim()));
  function renderItem(issue: SettlementPreflightIssue, index: number) {
    const href = issue.expense_id
      ? `/finance?tab=expenses&month=${encodeURIComponent(issue.expense_date?.slice(0, 7) ?? report!.billing_month)}&search=${encodeURIComponent(issue.expense_id)}`
      : issue.order_id ? `/orders?keyword=${encodeURIComponent(issue.order_id)}` : undefined;
    return (
      <li key={`${issue.code}-${issue.expense_id ?? issue.order_id ?? index}-${index}`} style={{ marginBottom: 16, overflowWrap: "anywhere" }}>
        <Text strong>{ISSUE_LABELS[issue.code] ?? issue.message}</Text>
        {ISSUE_LABELS[issue.code] && <div>{ISSUE_MESSAGES[issue.code] ?? issue.message}</div>}
        {issueReference(issue) && <div><Text type="secondary">{issueReference(issue)}</Text></div>}
        {issue.difference != null && <div>已登记 ¥{issue.current_amount} · 应登记 ¥{issue.expected_amount} · 差额 ¥{issue.difference}</div>}
        {issue.next_action && issue.next_action !== issue.message && <div>{issue.next_action}</div>}
        {href && <a href={href} style={{ display: "inline-flex", alignItems: "center", minHeight: 44 }}>查看相关{issue.expense_id ? "费用" : "订单"}</a>}
      </li>
    );
  }
  return (
    <Alert
      type={report.blocking ? "error" : missing.length ? "warning" : "success"}
      showIcon
      message={report.blocking ? `发现 ${report.issues.length} 项阻断问题，暂不能结算`
        : missing.length ? `有 ${missing.length} 笔服务费待入账，生成方案时会列出补录`
        : "月结体检通过，可以生成或确认结算"}
      description={<>
        <div>{report.billing_month} · 已检查费用承担方、续住关系、跨月记账、重复费用及平台差异。检查依据为当前系统记录。</div>
        {!!report.issues.length && <ul style={{ margin: "12px 0 0", paddingLeft: 20 }}>{report.issues.map(renderItem)}</ul>}
        {!!missing.length && <details><summary style={{ cursor: "pointer", minHeight: 44, paddingTop: 12 }}>查看 {missing.length} 笔待补费用</summary>
          <ul style={{ paddingLeft: 20 }}>{missing.map(renderItem)}</ul></details>}
        {!!explained.length && <details><summary style={{ cursor: "pointer", minHeight: 44, paddingTop: 12 }}>为什么订单条数、晚数和费用不一样（{explained.length} 笔说明）</summary>
          <Input aria-label="按姓名或房号查计费说明" placeholder="输入客人姓名、房号或费用项目" value={search} onChange={(event) => setSearch(event.target.value)} style={{ maxWidth: 420, margin: "8px 0", minHeight: 44 }} />
          <List dataSource={filtered} locale={{ emptyText: "没有找到匹配的计费说明，请换个姓名或房号。" }}
            pagination={filtered.length > 10 ? { pageSize: 10, size: "small", showSizeChanger: false, simple: true } : false}
            renderItem={(item, index) => <List.Item><ul style={{ paddingLeft: 20, margin: 0 }}>{renderItem(item, index)}</ul></List.Item>} />
        </details>}
      </>}
    />
  );
}
