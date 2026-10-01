"use client";

import { useRef, useState } from "react";
import { Alert, Button, Card, Input, Space, Tag } from "antd";
import { LoadingOutlined } from "@ant-design/icons";

import { extractErrorMessage } from "@/lib/api-errors";
import type { MonthlyCloseProposalCommand, MonthlyCloseProposalView } from "@/lib/monthly-close";

type ProposalCardProps = {
  proposal: MonthlyCloseProposalView;
  role: string;
  onApprove?: (requestId: string) => Promise<unknown>;
  onReject?: (reason: string, requestId: string) => Promise<unknown>;
  onExecute?: (requestId: string) => Promise<unknown>;
  onVerify?: (attemptId: string, requestId: string) => Promise<unknown>;
  onConfirmAndApply?: () => Promise<unknown>;
  onRegenerate?: () => Promise<unknown>;
  onEditRecipe?: () => void;
  sourceCompletionState?: "completed" | "needs_confirmation" | "difference";
};

const actionStyle = { minHeight: 44, borderRadius: 12, fontWeight: 500 } as const;

function stringValue(value: unknown) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function changeLabel(command: MonthlyCloseProposalCommand, proposalType: string) {
  const before = command.before;
  const after = command.after;
  if (proposalType === "operating_expense_import") {
    return `${stringValue(after.description)} · ${money(after.amount)}`;
  }
  if (proposalType === "utility_reconciliation") {
    const rows = Array.isArray(after.rows) ? after.rows.length : 0;
    return `${stringValue(after.billing_month)} 水电对账 · ${rows} 条原始明细`;
  }
  if (proposalType === "service_fee_reconciliation") {
    const fees = Array.isArray(after.fees) ? after.fees.length : 0;
    return `系统服务费 · ${fees} 笔确定性费用`;
  }
  if (before.action === "withdraw") {
    return "撤销错误申诉（金额不变）";
  }
  if (before.action === "confirm_identity") {
    return "确认保留准确申诉（金额不变）";
  }
  if (before.diff_class === "fix_amount") {
    return `${stringValue(before.order_ota_owner_revenue)} → ${stringValue(after.order_ota_owner_revenue)}`;
  }
  if (before.diff_class === "compensation") {
    return `赔款支出 ${stringValue(before.bill_amount)} · ${stringValue(before.status)} → ${stringValue(after.status)}`;
  }
  if (before.diff_class === "broken_link") {
    return `平台单关联 ${stringValue(before.link_matches)} → ${stringValue(after.link_matches)}`;
  }
  return `${stringValue(before.status)} → ${stringValue(after.status)}`;
}

function proposalTitle(proposalType: string) {
  return {
    ota_appeal_adjudication: "OTA 申诉裁决方案",
    ota_reconciliation: "OTA 调账方案",
    service_fee_reconciliation: "服务费方案",
    utility_reconciliation: "水电对账方案",
    operating_expense_import: "运营支出方案",
  }[proposalType] ?? "月结方案";
}

function hasValue(record: Record<string, unknown>, key: string) {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function money(value: unknown) {
  return value === null || value === undefined || value === "" ? "无" : `¥${String(value)}`;
}

function yesNo(value: unknown) {
  return value ? "是" : "否";
}

function roomRevenue(record: Record<string, unknown>) {
  const rows = record.room_ota_owner_revenues;
  if (!Array.isArray(rows) || rows.length === 0) return "无";
  return rows.map((row) => {
    const value = row && typeof row === "object" ? (row as Record<string, unknown>).value : null;
    return money(value);
  }).join("、");
}

function warningText(record: Record<string, unknown>) {
  const warnings = record.settlement_warnings;
  return Array.isArray(warnings) && warnings.length > 0 ? warnings.join("；") : "无";
}

function settlementScope(record: Record<string, unknown>) {
  const scope = record.settlement_scope;
  if (!Array.isArray(scope) || scope.length === 0) return "无";
  return scope.map((row) => {
    const item = row && typeof row === "object" ? row as Record<string, unknown> : {};
    return `${stringValue(item.settlement_id)}（${stringValue(item.status)}）`;
  }).join("、");
}

function breadcrumbSummary(value: unknown) {
  if (!value || typeof value !== "object") return "无";
  const item = value as Record<string, unknown>;
  return `已记录（原订单到账 ${money(item.old_rev)}，原补贴 ${money(item.old_sub)}，原房间到账 ${money(item.old_room_rev)}）`;
}

function compensationSummary(record: Record<string, unknown>) {
  const raw = record.compensation_expense;
  if (!raw || typeof raw !== "object") return null;
  const expense = raw as Record<string, unknown>;
  const present = expense.present ? "已入账" : "未入账";
  const category = expense.category === "other" ? "其他费用" : stringValue(expense.category);
  const payer = expense.payer === "company" ? "公司承担" : expense.payer === "owner" ? "业主承担" : stringValue(expense.payer);
  const creator = expense.created_by === "execution_actor"
    ? "本次执行人"
    : stringValue(expense.created_by);
  return `${present} · ${category} · ${stringValue(expense.expense_date)} · ${payer} · ${money(expense.amount)} · 订单 ${stringValue(expense.order_ref)} · 记账人 ${creator}`;
}

function compensationDedupe(record: Record<string, unknown>) {
  const raw = record.compensation_expense;
  if (!raw || typeof raw !== "object") return null;
  return Boolean((raw as Record<string, unknown>).deduped);
}

export function ProposalCard({
  proposal,
  role,
  onApprove,
  onReject,
  onExecute,
  onVerify,
  onConfirmAndApply,
  onRegenerate,
  onEditRecipe,
  sourceCompletionState,
}: ProposalCardProps) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const pendingRef = useRef<string | null>(null);
  const isOta = proposal.proposal_type === "ota_reconciliation"
    || proposal.proposal_type === "ota_appeal_adjudication";
  const requestPrefix = isOta ? "ota" : "monthly-close";
  const requestIds = useRef({
    approve: `${requestPrefix}:${proposal.proposal_id}:approve`,
    reject: `${requestPrefix}:${proposal.proposal_id}:reject`,
    execute: `${requestPrefix}:${proposal.proposal_id}:execute`,
    verify: `${requestPrefix}:${proposal.proposal_id}:verify`,
  });
  const commands = proposal.canonical_payload.commands;
  const attempt = proposal.attempts?.at(-1);
  const verification = proposal.verifications
    ?.filter((item) => !attempt || item.attempt_id === attempt.attempt_id)
    .at(-1);
  const isAdmin = role === "admin";
  const canConfirmAndApply = Boolean(onConfirmAndApply) && (
    proposal.status === "pending_approval"
    || (proposal.status === "approved" && !attempt)
    || (attempt?.status === "succeeded_unverified" && !verification)
  );
  const executionRetryNo = useRef(0);

  const run = async (key: string, action: () => Promise<unknown>) => {
    if (pendingRef.current) return;
    pendingRef.current = key;
    setPending(key);
    setError(null);
    try {
      await action();
    } catch (cause) {
      setError(extractErrorMessage(cause, "操作失败，请重试"));
    } finally {
      pendingRef.current = null;
      setPending(null);
    }
  };

  const reject = () => {
    const value = reason.trim();
    if (!value) {
      setError("请填写拒绝原因");
      return;
    }
    if (onReject) void run("reject", () => onReject(value, requestIds.current.reject));
  };

  const retryFailedSafe = () => {
    if (!onExecute) return;
    executionRetryNo.current += 1;
    const requestId = `${requestPrefix}:${proposal.proposal_id}:execute:retry:${executionRetryNo.current}:${Date.now()}`;
    void run("execute", () => onExecute(requestId));
  };

  const proposalState = {
    rejected: "方案已拒绝",
    stale: "事实已变化，原方案已失效",
    superseded: "已有更新方案替代本方案",
  }[proposal.status];
  const rejectionReason = proposal.approvals
    ?.filter((approval) => approval.decision === "rejected" && approval.reason?.trim())
    .at(-1)?.reason?.trim();

  const verificationState = verification?.status === "verified"
    ? sourceCompletionState && sourceCompletionState !== "completed"
      ? {
        type: "warning" as const,
        message: sourceCompletionState === "difference"
          ? "执行结果已复核；当前来源仍有差异"
          : "执行结果已复核；当前来源仍需确认",
      }
      : { type: "success" as const, message: "确定性复核通过，方案已完成" }
    : verification?.status === "failed"
      ? { type: "error" as const, message: "确定性复核失败，需管理员检查" }
      : verification?.status === "inconclusive"
        ? { type: "error" as const, message: "确定性复核无法确认，需管理员检查" }
        : null;

  const attemptState = !attempt || verificationState ? null : {
    pending: { type: "info" as const, message: "执行请求已受理，正在处理" },
    executing: { type: "info" as const, message: "执行请求已受理，正在处理" },
    succeeded_unverified: { type: "warning" as const, message: "执行已提交，等待确定性复核" },
    verified: { type: "success" as const, message: "确定性复核通过，方案已完成" },
    failed_safe: { type: "warning" as const, message: "执行未写入，可在确认事实未变化后安全重试" },
    failed_confirmed: { type: "error" as const, message: "执行失败且已确认，需管理员介入" },
    unknown: { type: "error" as const, message: "执行结果不确定，需要确定性检查/修复；禁止重放" },
    remediation_required: { type: "error" as const, message: "复核发现偏差，需管理员介入修复" },
  }[attempt.status] ?? { type: "error" as const, message: "执行处于未知状态，请管理员检查" };

  return (
    <Card
      title={`${proposalTitle(proposal.proposal_type)} · ${commands.length} 项变更`}
      extra={<Tag color="blue">金额影响 ¥{proposal.impact_snapshot.total_amount ?? "0.00"}</Tag>}
      style={{ borderRadius: 12 }}
      styles={{ body: { borderRadius: 12 } }}
    >
      <div style={{ display: "grid", gap: 12 }}>
        {commands.map((command, index) => (
          <section
            key={command.business_idempotency_key}
            aria-label={`变更 ${index + 1}`}
            style={{ border: "1px solid #eee", borderRadius: 12, padding: 12 }}
          >
            <div style={{ fontWeight: 500 }}>{changeLabel(command, proposal.proposal_type)}</div>
            <div>{isOta ? `OTA账单证据 · 第 ${index + 1} 条差异` : `原件与规则证据 · 第 ${index + 1} 项`}</div>
            {proposal.proposal_type === "ota_appeal_adjudication" && (
              <>
                <div>裁决原因：{stringValue(command.before.reason)}</div>
                <div>
                  申诉状态：{stringValue((command.before.target as Record<string, unknown>)?.status)} → {stringValue((command.after.target as Record<string, unknown>)?.status)}
                </div>
                {command.before.selected_identity && typeof command.before.selected_identity === "object" && (
                  <div>
                    隐私订单：{stringValue((command.before.selected_identity as Record<string, unknown>).channel)} · {stringValue((command.before.selected_identity as Record<string, unknown>).order_ref)} · 版本 {stringValue((command.before.selected_identity as Record<string, unknown>).order_version)}
                  </div>
                )}
                <div>
                  账单链路：原申诉 {stringValue(command.before.original_billing_month)} → 到账证据 {stringValue(command.before.later_billing_month)}
                </div>
                {command.before.source_row && typeof command.before.source_row === "object" && (
                  <div>
                    到账行：第 {stringValue((command.before.source_row as Record<string, unknown>).row_index)} 行 · {money((command.before.source_row as Record<string, unknown>).amount)} · {stringValue((command.before.source_row as Record<string, unknown>).checkin)} 至 {stringValue((command.before.source_row as Record<string, unknown>).checkout)} · 平台单 {stringValue((command.before.source_row as Record<string, unknown>).platform_order_ref)}
                  </div>
                )}
                {command.before.action === "withdraw" && (
                  <div>关闭原因：{stringValue((command.after.target as Record<string, unknown>)?.dismissal_reason)}</div>
                )}
              </>
            )}
            {(hasValue(command.before, "ota_subsidy") || hasValue(command.after, "ota_subsidy")) && (
              <div>平台补贴：{money(command.before.ota_subsidy)} → {money(command.after.ota_subsidy)}</div>
            )}
            {hasValue(command.before, "actual_price") && (
              <div>计价基数：{money(command.before.actual_price)} → {money(command.after.actual_price)}</div>
            )}
            {hasValue(command.before, "platform_commission_rate") && (
              <div>平台佣金率：{stringValue(command.before.platform_commission_rate)} → {stringValue(command.after.platform_commission_rate)}</div>
            )}
            {(hasValue(command.before, "price_pending") || hasValue(command.after, "price_pending")) && (
              <div>价格待确认：{yesNo(command.before.price_pending)} → {yesNo(command.after.price_pending)}</div>
            )}
            {(hasValue(command.before, "price_locked") || hasValue(command.after, "price_locked")) && (
              <div>价格锁定：{yesNo(command.before.price_locked)} → {yesNo(command.after.price_locked)}</div>
            )}
            {(hasValue(command.before, "room_ota_owner_revenues") || hasValue(command.after, "room_ota_owner_revenues")) && (
              <div>房间到账：{roomRevenue(command.before)} → {roomRevenue(command.after)}</div>
            )}
            {(hasValue(command.before, "reconciliation_breadcrumb") || hasValue(command.after, "reconciliation_breadcrumb")) && (
              <div>对账留痕：{breadcrumbSummary(command.before.reconciliation_breadcrumb)} → {breadcrumbSummary(command.after.reconciliation_breadcrumb)}</div>
            )}
            {isOta && hasValue(command.before, "billing_month") && (
              <div>账单月份：{stringValue(command.before.billing_month)} · 离店月份：{stringValue(command.before.checkout_month)} · 影响结算月：{stringValue(command.before.settlement_month)}</div>
            )}
            {(hasValue(command.before, "settlement_scope") || hasValue(command.after, "settlement_scope")) && (
              <div>结算范围：{settlementScope(command.before)} → {settlementScope(command.after)}</div>
            )}
            {(hasValue(command.before, "settlement_warnings") || hasValue(command.after, "settlement_warnings")) && (
              <div>结算影响：{warningText(command.before)} → {warningText(command.after)}</div>
            )}
            {(compensationSummary(command.before) || compensationSummary(command.after)) && (
              <div>赔款费用：{compensationSummary(command.before) ?? "无"} → {compensationSummary(command.after) ?? "无"}</div>
            )}
            {(compensationDedupe(command.before) !== null || compensationDedupe(command.after) !== null) && (
              <div>
                去重检查：{yesNo(compensationDedupe(command.before))} → {yesNo(compensationDedupe(command.after))}
                {compensationDedupe(command.after) ? "（已存在，执行不重复入账）" : ""}
              </div>
            )}
            {proposal.proposal_type !== "ota_appeal_adjudication" && command.after.dismissal_reason ? <div>关闭原因：{stringValue(command.after.dismissal_reason)}</div> : null}
            {command.after.target_order && typeof command.after.target_order === "object" ? (
              <div>
                认领目标：{stringValue((command.after.target_order as Record<string, unknown>).order_ref)}
                {" · 版本："}{stringValue((command.after.target_order as Record<string, unknown>).version)}
              </div>
            ) : null}
            <div>
              金额影响：¥{command.amount_impact}
              {proposal.proposal_type === "ota_appeal_adjudication" ? "（不会修改金额）" : ""}
            </div>
            <details>
              <summary>查看技术凭据</summary>
              {proposal.proposal_type === "ota_appeal_adjudication" ? (
                <>
                  <div>候选：{stringValue(command.before.candidate_id)}</div>
                  <div>问题：{stringValue(command.before.issue_id)}</div>
                  <div>原周期：{stringValue(command.before.original_cycle_id)}</div>
                  <div>原批次：{stringValue(command.before.original_batch_id)}</div>
                  <div>原批次状态：{stringValue(command.before.original_batch_status)}</div>
                  <div>原差异：{stringValue(command.before.original_diff_id)}</div>
                  <div>原方案：{stringValue(command.before.original_proposal_id)}</div>
                  <div>原执行：{stringValue(command.before.original_attempt_id)}</div>
                  <div>原复核：{stringValue(command.before.original_verification_id)}</div>
                  <div>原复核证据：{stringValue(command.before.original_verification_evidence_hash)}</div>
                  <div>到账周期：{stringValue(command.before.later_cycle_id)}</div>
                  <div>到账文档：{stringValue(command.before.later_document_id)}</div>
                  <div>文档 SHA：{stringValue(command.before.later_document_sha256)}</div>
                  <div>到账批次：{stringValue(command.before.later_batch_id)}</div>
                  <div>到账批次状态：{stringValue(command.before.later_batch_status)}</div>
                  <div>批次指纹：{stringValue(command.before.later_batch_fingerprint)}</div>
                  <div>行哈希：{stringValue((command.before.source_row as Record<string, unknown> | undefined)?.row_hash)}</div>
                </>
              ) : isOta ? (
                <>
                  <div>批次：{stringValue(command.before.batch_id)}</div>
                  <div>差异：{stringValue(command.before.diff_id)}</div>
                </>
              ) : (
                <>
                  {command.evidence_refs.map((reference, referenceIndex) => (
                    <div key={`${command.business_idempotency_key}-evidence-${referenceIndex}`}>
                      {stringValue(reference.kind)}：{stringValue(reference.document_id ?? reference.ruleset_version ?? reference.plan_hash ?? reference.content_hash)}
                    </div>
                  ))}
                </>
              )}
            </details>
          </section>
        ))}
        <div><span style={{ fontWeight: 500 }}>风险：</span>{proposal.impact_snapshot.risk ?? "将执行已批准的确定性写入"}</div>
        <div>
          <span style={{ fontWeight: 500 }}>审批：</span>
          {proposal.impact_snapshot.required_approver ?? proposal.approval_policy_snapshot.approval_roles?.join("、") ?? "管理员"}
        </div>
        <div><span style={{ fontWeight: 500 }}>执行后复核：</span>{proposal.impact_snapshot.verification ?? "重新读取业务事实后确认结果"}</div>

        {proposal.status === "pending_approval" && (
          <Alert
            type="info"
            showIcon
            message={onConfirmAndApply
              ? "请确认上面的调整；确认后助理会执行并检查结果"
              : "方案等待管理员审批，尚未写入"}
          />
        )}
        {proposal.status === "approved" && !attempt && (
          <Alert type="info" showIcon message="方案已批准，尚未执行" />
        )}
        {proposalState && <Alert type="warning" showIcon message={proposalState} />}
        {rejectionReason && <Alert type="warning" showIcon message={`拒绝原因：${rejectionReason}`} />}
        {verificationState && <Alert type={verificationState.type} showIcon message={verificationState.message} />}
        {attemptState && <Alert type={attemptState.type} showIcon message={attemptState.message} />}
        {error && <Alert type="error" showIcon message={error} />}

        {proposal.status === "rejected" && onEditRecipe && (
          <Button
            style={actionStyle}
            disabled={pending !== null}
            onClick={onEditRecipe}
          >
            修改处置后生成新方案
          </Button>
        )}

        {proposal.status !== "rejected" && proposalState && onRegenerate && (
          <Button
            style={actionStyle}
            loading={pending === "regenerate"}
            disabled={pending !== null}
            onClick={() => void run("regenerate", onRegenerate)}
          >
            根据最新事实重新生成
          </Button>
        )}

        {isAdmin && canConfirmAndApply && (
          <Button
            type="primary"
            style={actionStyle}
            loading={pending === "confirm-and-apply"}
            disabled={pending !== null}
            onClick={() => void run("confirm-and-apply", onConfirmAndApply!)}
          >
            确认并完成这项核对
          </Button>
        )}

        {isAdmin && proposal.status === "pending_approval" && (onApprove || onReject) && (
          <Space direction="vertical" style={{ width: "100%" }}>
            {onReject && (
              <Input.TextArea
                aria-label="拒绝原因"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="拒绝时必须填写原因"
                rows={2}
              />
            )}
            <Space wrap>
              {onApprove && !onConfirmAndApply && (
                <Button
                  type="primary"
                  style={actionStyle}
                  loading={pending === "approve"}
                  disabled={pending !== null}
                  onClick={() => void run("approve", () => onApprove(requestIds.current.approve))}
                >
                  批准方案
                </Button>
              )}
              {onReject && (
                <Button danger style={actionStyle} loading={pending === "reject"} disabled={pending !== null} onClick={reject}>
                  拒绝方案
                </Button>
              )}
            </Space>
          </Space>
        )}
        {isAdmin && proposal.status === "approved" && onExecute && !attempt && !onConfirmAndApply && (
          <Button
            type="primary"
            style={actionStyle}
            loading={pending === "execute"}
            disabled={pending !== null}
            onClick={() => void run("execute", () => onExecute(requestIds.current.execute))}
          >
            执行已批准方案
          </Button>
        )}
        {isAdmin && attempt?.status === "succeeded_unverified" && !verification && onVerify && !onConfirmAndApply && (
          <Button
            type="primary"
            style={actionStyle}
            loading={pending === "verify"}
            disabled={pending !== null}
            onClick={() => void run("verify", () => onVerify(attempt.attempt_id, requestIds.current.verify))}
          >
            运行确定性复核
          </Button>
        )}
        {isAdmin && attempt?.status === "failed_safe" && onExecute && (
          <Button
            type="primary"
            style={actionStyle}
            disabled={pending !== null}
            aria-busy={pending === "execute"}
            onClick={retryFailedSafe}
          >
            {pending === "execute" ? (
              <Space size={8} aria-live="polite">
                <span data-testid="ota-retry-spinner" aria-hidden="true">
                  <LoadingOutlined spin />
                </span>
                正在重试…
              </Space>
            ) : "安全重试执行"}
          </Button>
        )}
        {isAdmin && attempt?.status === "unknown" && onVerify && (
          <Button
            style={actionStyle}
            loading={pending === "verify"}
            disabled={pending !== null}
            onClick={() => void run("verify", () => onVerify(attempt.attempt_id, requestIds.current.verify))}
          >
            检查执行结果
          </Button>
        )}
      </div>
    </Card>
  );
}
