import { Alert, Spin, Typography } from "antd";

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
  if (loading) {
    return <Alert type="info" showIcon message={<><Spin size="small" /> 正在执行月结体检…</>} />;
  }
  if (!report) return null;
  if (!report.blocking) {
    return (
      <Alert
        type="success"
        showIcon
        message="月结体检通过，可以生成或确认结算"
        description={`${report.billing_month} 未发现佣金、费用归属或平台账单异常。`}
      />
    );
  }

  return (
    <Alert
      type="error"
      showIcon
      message={`发现 ${report.issues.length} 项阻断问题，暂不能结算`}
      description={(
        <ul style={{ margin: "8px 0 0", paddingLeft: 20 }}>
          {report.issues.map((issue, index) => (
            <li key={`${issue.code}-${issue.order_id ?? issue.expense_id ?? issue.recon_diff_id ?? index}`}>
              <Text strong>{ISSUE_LABELS[issue.code] ?? issue.code}</Text>
              {`：${ISSUE_MESSAGES[issue.code] ?? issue.message}`}
              {issueReference(issue) ? <div><Text type="secondary">{issueReference(issue)}</Text></div> : null}
            </li>
          ))}
        </ul>
      )}
    />
  );
}
