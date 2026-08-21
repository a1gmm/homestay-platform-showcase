import { Alert, Spin, Typography } from "antd";

import type { SettlementPreflightIssue, SettlementPreflightReport } from "@/lib/types";


const { Text } = Typography;

const ISSUE_LABELS: Record<string, string> = {
  nonplatform_commission: "线下单佣金残留",
  invalid_order_expense: "取消/删除订单费用",
  owner_self_owner_expense: "自住费用承担方错误",
  open_reconciliation: "平台账单差异",
  duplicate_platform_order: "重复平台订单号",
};

function issueReference(issue: SettlementPreflightIssue): string {
  return [
    issue.order_id && `订单 ${issue.order_id}`,
    issue.room_id && `房间 ${issue.room_id}`,
    issue.expense_id && `费用 ${issue.expense_id}`,
    issue.recon_diff_id && `差异 ${issue.recon_diff_id}`,
    issue.platform_order_id && `平台单 ${issue.platform_order_id}`,
    issue.amount && `金额 ¥${issue.amount}`,
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
              {`：${issue.message}`}
              {issueReference(issue) ? <div><Text type="secondary">{issueReference(issue)}</Text></div> : null}
            </li>
          ))}
        </ul>
      )}
    />
  );
}
