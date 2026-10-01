import { Alert, Button, Empty, Space } from "antd";
import { useRouter } from "next/navigation";

import { canProcessMonthlyCloseStepEarly } from "@/lib/monthly-close";
import type { MonthlyCloseCycle, MonthlyCloseDurableReceipt, MonthlyCloseInboxItem, MonthlyCloseIssue, MonthlyCloseStep } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";
import { SourceCollectionPanel } from "./SourceCollectionPanel";
import { OtaStatementPanel } from "./OtaStatementPanel";
import { ServiceMappingReview } from "./ServiceMappingReview";
import { OperatingExpenseMappingReview } from "./OperatingExpenseMappingReview";
import { SmartUploadInbox } from "./SmartUploadInbox";
import { UtilityMappingReview } from "./UtilityMappingReview";

type SettlementSummaryRow = {
  settlement_id: string;
  owner_id: string;
  total_net_revenue: string;
  deducted_expenses: string;
  actual_owner_amount: string;
};

type NavigateAction = NonNullable<MonthlyCloseIssue["action"]> & { kind: "navigate" };

type IssueGroup = {
  key: string;
  label: string;
  issues: MonthlyCloseIssue[];
  action?: NavigateAction;
};

function issueGroupLabel(issue: MonthlyCloseIssue) {
  const path = issue.action?.kind === "navigate" ? issue.action.path : "";
  if (path === "/orders" || issue.code.includes("order")) return "订单问题";
  if (path === "/finance/billing-recon" || issue.code.includes("ota")) return "OTA账单差异";
  if (path === "/finance/utility-recon" || issue.code.includes("utility")) return "水电对账问题";
  if (path === "/settlements" || issue.code.includes("settlement")) return "结算问题";
  if (issue.code.includes("service")) return "服务费问题";
  return "其他问题";
}

function sharedNavigateAction(issues: MonthlyCloseIssue[]): NavigateAction | undefined {
  const actions = issues
    .map((issue) => issue.action)
    .filter((action): action is NavigateAction => action?.kind === "navigate");
  const first = actions[0];
  if (!first) return undefined;
  const sharedQuery = Object.fromEntries(
    Object.entries(first.query || {}).filter(([key, value]) => (
      actions.every((action) => action.query?.[key] === value)
    )),
  );
  return { ...first, query: sharedQuery };
}

function groupIssues(issues: MonthlyCloseIssue[]): IssueGroup[] {
  const grouped = new Map<string, MonthlyCloseIssue[]>();
  for (const issue of issues) {
    const label = issueGroupLabel(issue);
    const path = issue.action?.kind === "navigate" ? issue.action.path : "";
    const key = `${label}-${path}`;
    grouped.set(key, [...(grouped.get(key) || []), issue]);
  }
  return Array.from(grouped, ([key, groupedIssues]) => ({
    key,
    label: issueGroupLabel(groupedIssues[0]),
    issues: groupedIssues,
    action: sharedNavigateAction(groupedIssues),
  }));
}

function settlementRows(summary: Record<string, unknown>): SettlementSummaryRow[] {
  const rows = summary.settlements;
  if (!Array.isArray(rows)) return [];
  return rows.filter((row): row is SettlementSummaryRow => {
    if (!row || typeof row !== "object") return false;
    const value = row as Record<string, unknown>;
    return ["settlement_id", "owner_id", "total_net_revenue", "deducted_expenses", "actual_owner_amount"]
      .every((key) => typeof value[key] === "string");
  });
}

function yuan(value: string) {
  return Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function issueDescription(issue: MonthlyCloseStep["issues"][number]) {
  const failures = Array.isArray(issue.failed)
    ? issue.failed.filter((item): item is { row: number; errors: string } => (
      !!item
      && typeof item === "object"
      && typeof (item as Record<string, unknown>).row === "number"
      && typeof (item as Record<string, unknown>).errors === "string"
    ))
    : [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      {issue.impact && <div><strong>影响：</strong>{issue.impact}</div>}
      {issue.cause && <div><strong>原因：</strong>{issue.cause}</div>}
      {issue.next_step && <div><strong>下一步：</strong>{issue.next_step}</div>}
      {failures.length > 0 && (
        <ul style={{ margin: "3px 0 0", paddingLeft: 18 }}>
          {failures.map((failure) => <li key={`${failure.row}-${failure.errors}`}>{failure.errors}</li>)}
        </ul>
      )}
      {!issue.impact && !issue.cause && !issue.next_step && failures.length === 0 && <div>{issue.message}</div>}
    </div>
  );
}

export function CurrentStepPanel({
  cycle,
  step,
  confirming,
  uploading,
  engineRunning,
  readOnly,
  inboxItems,
  onSelectStep,
  onConfirm,
  onUpload,
  onReceiveInbox,
  onClassifyInbox,
  onSetInboxSource,
  onConfirmInbox,
  onPermanentDeleteInbox,
  onNotApplicable,
  onArchive,
  onImportOperatingExpenses,
  onReconcileServiceFees,
  onGenerateSettlements,
  onRefresh,
}: {
  cycle: MonthlyCloseCycle;
  step: MonthlyCloseStep | undefined;
  confirming: boolean;
  uploading: boolean;
  engineRunning: boolean;
  readOnly?: boolean;
  inboxItems: MonthlyCloseInboxItem[];
  onSelectStep?: (stepKey: string) => void;
  onConfirm: () => Promise<unknown>;
  onUpload: (sourceType: string, file: File) => Promise<unknown>;
  onReceiveInbox: (file: File) => Promise<MonthlyCloseDurableReceipt>;
  onClassifyInbox: (itemId: string) => Promise<unknown>;
  onSetInboxSource: (itemId: string, sourceType: string) => Promise<unknown>;
  onConfirmInbox: (itemId: string) => Promise<unknown>;
  onPermanentDeleteInbox?: (itemId: string) => Promise<unknown>;
  onNotApplicable: (sourceType: string, reason: string) => Promise<unknown>;
  onArchive: (documentId: string) => Promise<unknown>;
  onImportOperatingExpenses: () => Promise<unknown>;
  onReconcileServiceFees: () => Promise<unknown>;
  onGenerateSettlements: () => Promise<unknown>;
  onRefresh: () => Promise<unknown>;
}) {
  const router = useRouter();
  if (!step) return <Empty description="本月九步已全部完成" />;
  const isWorkflowStep = step.step_key === cycle.current_step;
  const canWorkAhead = step.status === "locked" && canProcessMonthlyCloseStepEarly(step.step_key);
  const waitingForPrevious = step.status === "locked" && !canWorkAhead;
  const staleWaitingForPrevious = step.status === "stale" && !isWorkflowStep;
  const operationDisabled = readOnly || waitingForPrevious || staleWaitingForPrevious || step.status === "confirmed";
  const currentStep = cycle.steps.find((candidate) => candidate.step_key === cycle.current_step);
  const currentStepLabel = currentStep?.label || "当前步骤";
  const canNavigateToIssues = !readOnly && !waitingForPrevious && !staleWaitingForPrevious && step.status !== "confirmed";
  const actionPath = step.step_key === "utilities"
    ? "/finance/utility-recon"
    : step.step_key === "ota_statements"
      ? "/finance/billing-recon"
      : null;
  const returnTo = `/finance/monthly-close?month=${cycle.billing_month}`;
  const serviceReviewDocuments = step.step_key === "service_fees"
    ? cycle.sources
      .filter((source) => source.source_type === "cleaning_statement" || source.source_type === "linen_statement")
      .flatMap((source) => source.documents.map((document) => ({ document, sourceType: source.source_type })))
      .filter(({ document }) => document.processing_status === "rejected")
    : [];
  const serviceReplacementDocumentIds = new Set(
    step.step_key === "service_fees"
      ? step.issues
        .filter((issue) => issue.code.startsWith("service_line_") && typeof issue.document_id === "string")
        .map((issue) => issue.document_id as string)
      : [],
  );
  const serviceReplacementSources = step.step_key === "service_fees"
    ? cycle.sources
      .filter((source) => source.source_type === "cleaning_statement" || source.source_type === "linen_statement")
      .map((source) => ({
        ...source,
        documents: source.documents.filter((document) => serviceReplacementDocumentIds.has(document.document_id)),
      }))
      .filter((source) => source.documents.length > 0)
    : [];
  const settlements = step.step_key === "settlement_review"
    ? settlementRows(step.summary)
    : [];
  const operatingSource = cycle.sources.find((source) => source.source_type === "operating_expenses");
  const operatingFailures = step.step_key === "utilities"
    ? step.issues.filter((issue) => issue.code === "operating_expense_import_failed")
    : [];
  const operatingPendingDocuments = step.step_key === "utilities"
    ? operatingSource?.documents.filter((document) => !document.engine_id) ?? []
    : [];
  const utilityDocuments = step.step_key === "utilities"
    ? cycle.sources
      .filter((source) => source.source_type === "utility_receipt" || source.source_type === "utility_expense")
      .flatMap((source) => source.documents.map((document) => ({ document, sourceType: source.source_type })))
      .filter(({ document }) => !document.engine_id)
    : [];
  const navigateActions = step.issues
    .map((issue) => issue.action)
    .filter((action): action is NonNullable<typeof action> => action?.kind === "navigate")
    .filter((action, index, actions) => actions.findIndex((candidate) => (
      candidate.path === action.path
      && candidate.label === action.label
      && JSON.stringify(candidate.query || {}) === JSON.stringify(action.query || {})
    )) === index);
  const navigableIssueCount = step.issues.filter((issue) => issue.action?.kind === "navigate").length;
  const showIssueOverview = step.issues.length > 0
    && (waitingForPrevious || staleWaitingForPrevious || navigableIssueCount > 1);
  const issueGroups = showIssueOverview ? groupIssues(step.issues) : [];
  const navigate = (action: (typeof navigateActions)[number]) => {
    const params = new URLSearchParams(action.query || {});
    params.set("returnTo", returnTo);
    router.push(`${action.path}?${params.toString()}`);
  };
  const stepInstruction = step.status === "confirmed"
    ? "已完成，下面内容仅供查看。"
    : waitingForPrevious
      ? `请先完成“${currentStepLabel}”，再处理这一步。`
      : staleWaitingForPrevious
        ? `请先完成“${currentStepLabel}”，再回来重新检查。`
        : canWorkAhead
          ? `已有资料可以先做；“${currentStepLabel}”完成后再确认这一步。`
          : step.blocking_count > 0
            ? `先处理下面 ${step.blocking_count} 项问题，处理完回到这里确认。`
            : "检查已通过，可以确认这一步。";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 16,
          flexWrap: "wrap",
          paddingBottom: 16,
          borderBottom: `0.5px solid ${tokens.anyu.color.linen}`,
        }}
      >
        <div style={{ minWidth: 0 }}>
          <h2 className="serif" style={{ margin: 0, fontSize: 22, lineHeight: 1.4 }}>{step.label}</h2>
          <div style={{ marginTop: 4, color: tokens.color.text.secondary, lineHeight: 1.6 }}>{stepInstruction}</div>
        </div>
        {(waitingForPrevious || staleWaitingForPrevious) && cycle.current_step && (
          <Button style={{ flex: "none" }} onClick={() => onSelectStep?.(cycle.current_step as string)}>
            回到{currentStepLabel} →
          </Button>
        )}
      </header>
      {step.step_key === "source_collection" ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <SmartUploadInbox
            items={inboxItems}
            disabled={operationDisabled}
            onReceive={onReceiveInbox}
            onClassify={onClassifyInbox}
            onSetSource={onSetInboxSource}
            onConfirm={onConfirmInbox}
            onPermanentDelete={onPermanentDeleteInbox}
          />
          <SourceCollectionPanel
            sources={cycle.sources}
            billingMonth={cycle.billing_month}
            uploading={uploading}
            readOnly={operationDisabled}
            showUploadActions={false}
            onRefresh={onRefresh}
            onUpload={onUpload}
            onNotApplicable={onNotApplicable}
            onArchive={onArchive}
          />
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {step.step_key === "service_fees" && (
            <Button id="service-fee-reconcile" disabled={operationDisabled} loading={engineRunning} onClick={() => void onReconcileServiceFees()}>
              补齐系统服务费并重新匹配供应商明细 →
            </Button>
          )}
          {serviceReviewDocuments.length > 0 && (
            <section
              aria-label="待确认的保洁布草表格结构"
              style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, padding: 16 }}
            >
              <div className="serif" style={{ fontSize: 18, marginBottom: 10 }}>先确认供应商表格结构</div>
              <div style={{ color: tokens.color.text.secondary, marginBottom: 12 }}>
                系统无法安全识别以下文件。请在本步骤确认工作表和列号，导入完成后再继续核对。
              </div>
              <Space direction="vertical" size={10} style={{ width: "100%" }}>
                {serviceReviewDocuments.map(({ document, sourceType }) => (
                  <div key={document.document_id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                    <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{document.filename}</span>
                    <ServiceMappingReview
                      billingMonth={cycle.billing_month}
                      document={document}
                      sourceType={sourceType}
                      disabled={operationDisabled}
                      onFinished={onRefresh}
                    />
                  </div>
                ))}
              </Space>
            </section>
          )}
          {serviceReplacementSources.length > 0 && (
            <section
              id="service-statement-replacement"
              aria-label="供应商明细修正"
              style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, padding: 16 }}
            >
              <div className="serif" style={{ fontSize: 18, marginBottom: 6 }}>替换有差异的供应商原件</div>
              <div style={{ color: tokens.color.text.secondary, marginBottom: 12 }}>
                先归档有差异的原件，再上传修正版；系统会重新匹配保洁、布草与服务费。
              </div>
              <SourceCollectionPanel
                sources={serviceReplacementSources}
                billingMonth={cycle.billing_month}
                uploading={uploading}
                readOnly={operationDisabled}
                uploadLabel="上传修正版"
                onRefresh={onRefresh}
                onUpload={onUpload}
                onNotApplicable={onNotApplicable}
                onArchive={onArchive}
              />
            </section>
          )}
          {utilityDocuments.length > 0 && (
            <section id="utility-expense" aria-label="水电表格结构检查" style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, padding: 16 }}>
              <div className="serif" style={{ fontSize: 18, marginBottom: 6 }}>水电原表格式检查</div>
              <div style={{ color: tokens.color.text.secondary, marginBottom: 12 }}>
                可直接开始对账；若系统提示无法认列，在这里人工确认一次，以后同格式自动沿用。
              </div>
              <Space direction="vertical" size={10} style={{ width: "100%" }}>
                {utilityDocuments.map(({ document, sourceType }) => (
                  <div key={document.document_id} style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
                    <span>{document.filename}</span>
                    <UtilityMappingReview
                      billingMonth={cycle.billing_month}
                      document={document}
                      sourceType={sourceType}
                      disabled={operationDisabled}
                      onFinished={onRefresh}
                    />
                  </div>
                ))}
              </Space>
            </section>
          )}
          {operatingPendingDocuments.length > 0 && (
            <section
              aria-label="待识别的运营支出原表"
              style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, padding: 16 }}
            >
              <div className="serif" style={{ fontSize: 18, marginBottom: 6 }}>识别并导入运营支出原表</div>
              <div style={{ color: tokens.color.text.secondary, marginBottom: 12, lineHeight: 1.7 }}>
                不用修改供应商或员工现有表格。DeepSeek 会识别列的含义，系统再重新校验日期、类别和金额；确认前不会入账。
              </div>
              <Space direction="vertical" size={10} style={{ width: "100%" }}>
                {operatingPendingDocuments.map((document) => (
                  <div
                    key={document.document_id}
                    style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}
                  >
                    <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{document.filename}</span>
                    <OperatingExpenseMappingReview
                      billingMonth={cycle.billing_month}
                      document={document}
                      disabled={operationDisabled}
                      onFinished={onRefresh}
                    />
                  </div>
                ))}
              </Space>
            </section>
          )}
          {step.step_key === "utilities" && operatingSource && operatingFailures.length > 0 && (
            <section
              id="operating-expenses"
              aria-label="运营支出文件修正"
              style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, padding: 16 }}
            >
              <div className="serif" style={{ fontSize: 18, marginBottom: 6 }}>替换导入失败的运营支出原件</div>
              <div style={{ color: tokens.color.text.secondary, marginBottom: 12 }}>
                先归档错误原件，再上传修正版；归档会同步作废该原件已经导入的费用，避免重复扣款。
              </div>
              {operatingFailures.map((issue) => (
                <Alert
                  key={`${issue.code}-${issue.resource_id}`}
                  style={{ marginBottom: 12 }}
                  type="warning"
                  showIcon
                  message={issue.message}
                  description={issueDescription(issue)}
                />
              ))}
              <SourceCollectionPanel
                sources={[operatingSource]}
                billingMonth={cycle.billing_month}
                uploading={uploading}
                readOnly={operationDisabled}
                uploadLabel="上传修正版"
                onRefresh={onRefresh}
                onUpload={onUpload}
                onNotApplicable={onNotApplicable}
                onArchive={onArchive}
              />
            </section>
          )}
          {step.step_key === "utilities" && operatingPendingDocuments.length === 0 && cycle.sources
            .find((source) => source.source_type === "operating_expenses")
            ?.documents.some((document) => document.processing_status === "rejected") && (
              <Button disabled={operationDisabled} loading={engineRunning} onClick={() => void onImportOperatingExpenses()}>
                重试导入已修正的运营支出 →
              </Button>
            )}
          {step.step_key === "ota_statements" && (
            <OtaStatementPanel
              billingMonth={cycle.billing_month}
              documents={cycle.sources.find((source) => source.source_type === "ota_statement")?.documents ?? []}
              disabled={operationDisabled}
              onFinished={onRefresh}
            />
          )}
          {step.step_key === "settlement_review" && isWorkflowStep && step.issues.some((issue) => issue.code === "settlements_missing") && (
            <Button disabled={operationDisabled || waitingForPrevious || !isWorkflowStep} loading={engineRunning} onClick={() => void onGenerateSettlements()}>
              生成本月全部业主结算单 →
            </Button>
          )}
          {step.step_key === "settlement_review" && settlements.length > 0 && (
            <section
              aria-label="结算复核证据"
              style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, padding: 16 }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 12 }}>
                <div>
                  <div className="serif" style={{ fontSize: 18 }}>本月结算金额证据</div>
                  <div style={{ color: tokens.color.text.secondary, marginTop: 4 }}>共 {settlements.length} 份，确认前逐份查看明细</div>
                </div>
                <Button
                  onClick={() => router.push(`/settlements?month=${cycle.billing_month}&returnTo=${encodeURIComponent(returnTo)}`)}
                >
                  查看并复核本月结算单 →
                </Button>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {settlements.map((settlement) => (
                  <div
                    key={settlement.settlement_id}
                    style={{ display: "grid", gridTemplateColumns: "minmax(90px, 1fr) repeat(3, minmax(100px, auto))", gap: 12, paddingTop: 8, borderTop: `0.5px solid ${tokens.anyu.color.linen}`, overflowX: "auto" }}
                  >
                    <span>业主 {settlement.owner_id}</span>
                    <span>净收入 ¥{yuan(settlement.total_net_revenue)}</span>
                    <span>扣除支出 ¥{yuan(settlement.deducted_expenses)}</span>
                    <span className="serif">实际应得 ¥{yuan(settlement.actual_owner_amount)}</span>
                  </div>
                ))}
              </div>
            </section>
          )}
          {showIssueOverview && (
            <section
              aria-label="待处理问题"
              style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, overflow: "hidden" }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "12px 16px", background: tokens.anyu.color.sand }}>
                <strong>待处理问题</strong>
                <span style={{ color: tokens.color.text.secondary }}>共 {step.issues.length} 项</span>
              </div>
              {issueGroups.map((group) => {
                const previews = Array.from(new Set(group.issues.map((issue) => issue.subject || issue.message)));
                return (
                  <div
                    key={group.key}
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      alignItems: "center",
                      gap: 16,
                      padding: "12px 16px",
                      borderTop: `0.5px solid ${tokens.anyu.color.linen}`,
                    }}
                  >
                    <div style={{ minWidth: 0, flex: "1 1 280px" }}>
                      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                        <strong>{group.label}</strong>
                        <span style={{ color: tokens.color.text.secondary, fontSize: 12 }}>{group.issues.length} 项</span>
                      </div>
                      <div style={{ marginTop: 4, color: tokens.color.text.secondary, fontSize: 13, lineHeight: 1.6 }}>
                        {previews.slice(0, 3).join("、")}
                        {previews.length > 3 ? `，另有 ${previews.length - 3} 项` : ""}
                      </div>
                    </div>
                    {canNavigateToIssues && group.action && (
                      <Button style={{ flex: "none" }} onClick={() => navigate(group.action as (typeof navigateActions)[number])}>
                        处理{group.label}{group.issues.length > 1 ? `（${group.issues.length} 项）` : ""} →
                      </Button>
                    )}
                  </div>
                );
              })}
            </section>
          )}
          {showIssueOverview || step.status === "locked" || staleWaitingForPrevious || step.status === "confirmed" || step.issues.length === 0 ? null : step.issues.map((issue) => (
            <Alert key={`${issue.code}-${issue.resource_id}`} type="warning" showIcon message={issue.subject || issue.message} description={issueDescription(issue)} />
          ))}
          {!showIssueOverview && canNavigateToIssues && navigateActions.map((action) => (
            <Button key={`${action.path}-${action.label}-${JSON.stringify(action.query || {})}`} onClick={() => navigate(action)}>
              {action.label}
            </Button>
          ))}
          {canNavigateToIssues && navigateActions.length === 0 && actionPath && step.issues.length > 0 && !step.issues.every((issue) => issue.code.endsWith("unprocessed")) && (
            <Button onClick={() => router.push(`${actionPath}?returnTo=${encodeURIComponent(returnTo)}`)}>
              在本流程中处理 →
            </Button>
          )}
          {canNavigateToIssues && navigateActions.length === 0 && step.step_key === "settlement_review" && settlements.length === 0 && step.issues.length > 0 && !step.issues.some((issue) => issue.code === "settlements_missing") && (
            <Button onClick={() => router.push(`/settlements?month=${cycle.billing_month}&returnTo=${encodeURIComponent(returnTo)}`)}>
              在结算页查看并处理 →
            </Button>
          )}
        </div>
      )}

      {isWorkflowStep && (step.confirmation_items?.length ?? 0) > 0 && (
        <section aria-label="本步骤确认内容" style={{ borderTop: `0.5px solid ${tokens.anyu.color.linen}`, paddingTop: 14 }}>
          <div style={{ color: tokens.color.text.secondary, marginBottom: 7 }}>点击后将确认：</div>
          <ul style={{ margin: 0, paddingLeft: 20 }}>
            {step.confirmation_items?.map((item) => <li key={item} style={{ marginBottom: 4 }}>{item}</li>)}
          </ul>
        </section>
      )}

      {isWorkflowStep && step.status !== "confirmed" && (
        <Space style={{ justifyContent: "flex-end" }}>
          <Button
            type="primary"
            disabled={readOnly || step.blocking_count > 0 || !["ready", "stale"].includes(step.status)}
            loading={confirming}
            onClick={() => void onConfirm()}
          >
            {step.confirmation_title || "确认本步骤"}
          </Button>
        </Space>
      )}
    </div>
  );
}
