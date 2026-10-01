"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import dayjs from "dayjs";
import dynamic from "next/dynamic";
import { Alert, Button, Drawer, Empty, Input, Modal, Skeleton, message } from "antd";
import { useRouter } from "next/navigation";

import { PageHeader } from "@/components/ui/PageHeader";
import { MonthlyCloseWorkspace, type DocumentRemovalResult } from "@/components/monthly-close/MonthlyCloseWorkspace";
import { CloseProgress } from "@/components/monthly-close/CloseProgress";
import { CurrentStepPanel } from "@/components/monthly-close/CurrentStepPanel";
import { IntakeLinkManager } from "@/components/monthly-close/IntakeLinkManager";
import { LayoutMemoryManager } from "@/components/monthly-close/LayoutMemoryManager";
import { MonthlyCloseControlTower } from "@/components/monthly-close/MonthlyCloseControlTower";
import { SmartUploadInbox } from "@/components/monthly-close/SmartUploadInbox";
import { OtaStatementPanel } from "@/components/monthly-close/OtaStatementPanel";
import { OperatingExpenseMappingReview } from "@/components/monthly-close/OperatingExpenseMappingReview";
import { ServiceMappingReview } from "@/components/monthly-close/ServiceMappingReview";
import { UtilityMappingReview } from "@/components/monthly-close/UtilityMappingReview";
import { SystemServiceFeeReview } from "@/components/monthly-close/SystemServiceFeeReview";
import { useAuthStore } from "@/lib/auth";
import { usePrivacyMode } from "@/hooks/usePrivacyMode";
import { useMonthlyClose } from "@/hooks/useMonthlyClose";
import { extractErrorMessage } from "@/lib/api-errors";
import { monthlyCloseApi, ordersApi } from "@/lib/api";
import { tokens } from "@/lib/design-tokens";
import { resolveMonthlyCloseAction, type MonthlyCloseActionDestination } from "@/lib/monthly-close-actions";
import type { MonthlyCloseCycle, MonthlyCloseDocument, MonthlyCloseProjectedDocument, MonthlyCloseProjectedDocumentSummary, MonthlyCloseStep } from "@/lib/monthly-close";

import { FinishMonthReview } from "@/components/monthly-close/FinishMonthReview";
import { EvidencePanel } from "@/components/monthly-close/EvidencePanel";
import { chatWorkflowTarget } from "@/components/monthly-close/ChatWorkflowActions";
import type { OrderOut } from "@/lib/types";

import { applySourceProposalQueue } from "@/lib/monthly-close-source-queue";

const EditOrderModal = dynamic(() => import("@/components/orders/EditOrderModal"), { ssr: false });

const WORKSPACE_ROLES = new Set(["admin", "finance", "operator", "cleaner", "keeper"]);
const EMPTY_LAYOUT_METRICS = { memory_count: 0, file_count: 0, deterministic_recognition_count: 0, ai_suggestion_count: 0, remembered_layout_count: 0, administrator_correction_count: 0, needs_confirmation_count: 0, active_memory_count: 0, disabled_memory_count: 0, reuse_count: 0, automation_rate: 0 };

function statusOf(error: unknown) {
  return (error as { response?: { status?: number } } | null)?.response?.status;
}

function manualMonthStatus(
  month: string,
  cycle: MonthlyCloseCycle | null | undefined,
  step: MonthlyCloseStep | undefined,
) {
  const monthLabel = dayjs(`${month}-01`).format("YYYY年MM月");
  if (cycle === undefined) return `${monthLabel} · 正在读取`;
  if (cycle === null) return `${monthLabel} · 尚未开始`;
  if (cycle.status === "completed") return `${monthLabel} · 已完成`;
  if (cycle.status === "needs_recheck") return `${monthLabel} · 数据有变化，需重新核对`;
  if (!step) return `${monthLabel} · 已完成`;
  if (step.blocking_count > 0) {
    const pending = step.step_key === "source_collection"
      ? `还缺 ${step.blocking_count} 项`
      : `待处理 ${step.blocking_count} 项`;
    return `${monthLabel} · ${step.label} · ${pending}`;
  }
  return `${monthLabel} · ${step.label} · 可以确认`;
}

export default function MonthlyClosePage() {
  const privacyMode = usePrivacyMode();
  const router = useRouter();
  const { user, session_id } = useAuthStore();
  const role = user?.role ?? "";
  const isAdmin = role === "admin";
  const canUseWorkspace = WORKSPACE_ROLES.has(role);
  const fileInput = useRef<HTMLInputElement>(null);
  const [month, setMonth] = useState(() => {
    if (typeof window !== "undefined") {
      const requested = new URLSearchParams(window.location.search).get("month");
      if (requested && /^\d{4}-(0[1-9]|1[0-2])$/.test(requested)) return requested;
    }
    return dayjs().subtract(1, "month").format("YYYY-MM");
  });
  const [managementOpen, setManagementOpen] = useState(false);
  const [adminAdvancedOpen, setAdminAdvancedOpen] = useState(false);
  const [selectedStepKey, setSelectedStepKey] = useState<string | null>(null);
  const [reopenOpen, setReopenOpen] = useState(false);
  const [reopenReason, setReopenReason] = useState("");
  const [uploadSourceType, setUploadSourceType] = useState<string | null>(null);
  const [selectedActionDetail, setSelectedActionDetail] = useState<{ scopeKey: string; destination: MonthlyCloseActionDestination } | null>(null);
  const [selectedReview, setSelectedReview] = useState<{ scopeKey: string; sourceType: string; mode: "review" | "read_only"; detail: MonthlyCloseProjectedDocument } | null>(null);
  const reviewRequestGeneration = useRef(0);
  const orderRequestGeneration = useRef(0);
  const [activeWorkflow, setActiveWorkflow] = useState<{ scopeKey: string; focus: string } | null>(null);
  const [selectedOrder, setSelectedOrder] = useState<{ scopeKey: string; order: OrderOut } | null>(null);
  const [orderStatus, setOrderStatus] = useState<{ scopeKey: string; message: string; error?: boolean } | null>(null);
  const activeScope = useRef("");
  const close = useMonthlyClose(month, canUseWorkspace, managementOpen, isAdmin, user && session_id ? {
    channel: "dashboard",
    sessionId: session_id,
    userId: user.user_id,
    role: user.role,
  } : undefined);
  const rawProjection = close.visibleProjection?.billing_month === month ? close.visibleProjection : null;
  const projection = rawProjection
    ? applySourceProposalQueue(rawProjection, close.sourceProposals.data)
    : null;
  const assistantEnabled = projection === null || projection.features.assistant_enabled;
  const workflowStep = useMemo(() => {
    const data = close.cycle.data;
    if (!data) return undefined;
    return data.steps.find((step) => step.step_key === data.current_step)
      ?? data.steps.find((step) => step.status !== "confirmed");
  }, [close.cycle.data]);
  const displayedStep = useMemo(() => {
    const data = close.cycle.data;
    if (!data) return undefined;
    return data.steps.find((step) => step.step_key === selectedStepKey) ?? workflowStep;
  }, [close.cycle.data, selectedStepKey, workflowStep]);
  const currentScopeKey = projection ? `${session_id ?? "signed-out"}:${user?.user_id ?? ""}:${projection.cycle_id}:${role}:${projection.actor_role}` : "";
  useLayoutEffect(() => {
    activeScope.current = currentScopeKey;
    return () => { activeScope.current = ""; };
  }, [currentScopeKey]);
  const setActionDetail = useCallback((destination: MonthlyCloseActionDestination | null) => {
    setSelectedActionDetail(destination ? { scopeKey: currentScopeKey, destination } : null);
  }, [currentScopeKey]);
  const scopedAction = selectedActionDetail?.scopeKey === currentScopeKey ? selectedActionDetail.destination : null;
  const actionDetail: MonthlyCloseActionDestination | null = scopedAction && (scopedAction.kind === "source_workflow" || scopedAction.kind === "source_review")
    && !projection?.sources.some((source) => source.source_id === scopedAction.sourceId && source.source_type === scopedAction.sourceType)
    ? { kind: "invalid", message: "该来源已不属于当前月结资料，请刷新后重新选择。" }
    : scopedAction;
  const renderedWorkflow = isAdmin && activeWorkflow?.scopeKey === currentScopeKey ? activeWorkflow : null;
  const renderedOrder = isAdmin && close.cycle.data?.stored_status !== "completed" && projection?.cycle_status !== "completed" && selectedOrder?.scopeKey === currentScopeKey ? selectedOrder : null;
  const renderedReview = selectedReview
    && selectedReview.scopeKey === currentScopeKey
    && projection?.sources.some((source) => source.documents.some((document) => document.document_id === selectedReview.detail.document_id))
      ? selectedReview
      : null;

  useEffect(() => {
    reviewRequestGeneration.current += 1;
    orderRequestGeneration.current += 1;
    setActiveWorkflow(null);
    setSelectedOrder(null);
    setOrderStatus(null);
    setSelectedReview(null);
    setSelectedActionDetail(null);
  }, [currentScopeKey]);

  const selectMonth = (next: string) => {
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(next)) return;
    setSelectedStepKey(null);
    setMonth(next);
    window.history.replaceState(null, "", `/finance/monthly-close?month=${next}`);
  };

  if (user && !canUseWorkspace) {
    router.replace("/dashboard");
    return null;
  }
  if (!user) return null;

  const documentById = (documentId: string) => projection?.sources.flatMap((source) => source.documents).find((document) => document.document_id === documentId);
  const openDocumentReview = async (documentId: string, mode: "review" | "read_only" = "review", useChat = true) => {
    const document = documentById(documentId);
    if (!document || !currentScopeKey) return;
    const generation = ++reviewRequestGeneration.current;
    const scopeKey = currentScopeKey;
    setActiveWorkflow(null);
    if (useChat && assistantEnabled && isAdmin && document.source_type === "cleaning_statement") {
      if (close.sendMessage.isPending) return;
      setSelectedReview(null); setActionDetail(null);
      try {
        const response = await close.sendMessage.mutateAsync({ text: "查看这份保洁表的核对结果，请在聊天里告诉我差异和处理方案", attachmentIds: [documentId] });
        if (reviewRequestGeneration.current !== generation || activeScope.current !== scopeKey || response.data.tool === "cleaning_work_chat") return;
      } catch (error) {
        if (reviewRequestGeneration.current === generation && activeScope.current === scopeKey) message.error(extractErrorMessage(error, "核对没有完成，请重新发送"));
        return;
      }
    }
    try {
      const detail = await close.loadEvidence(documentId);
      if (reviewRequestGeneration.current !== generation || activeScope.current !== scopeKey) return;
      setSelectedReview({ scopeKey, sourceType: document.source_type, mode, detail });
      setActionDetail(null);
    } catch (error) {
      if (reviewRequestGeneration.current === generation && activeScope.current === scopeKey) message.error(extractErrorMessage(error, "资料详情加载失败，请刷新后重试"));
    }
  };
  const openWorkflow = (focus: string) => {
    if (!isAdmin || !projection || !currentScopeKey) return;
    const target = chatWorkflowTarget(projection, focus);
    if (target.reason) { message.warning(target.reason); return; }
    reviewRequestGeneration.current += 1;
    setSelectedReview(null);
    setActionDetail(null);
    setActiveWorkflow(null);
    const systemFees = focus === "service_fees" ? projection.sources.find((item) => item.source_type === "system_service_fees") : undefined;
    if (systemFees) {
      setActionDetail({ kind: "source_workflow", sourceId: systemFees.source_id, sourceType: systemFees.source_type });
    } else if (target.source) {
      setActionDetail({ kind: "source_review", sourceId: target.source.source_id,
        sourceType: target.source.source_type, documentIds: target.source.documents.map((item) => item.document_id), mode: projection.cycle_status === "completed" ? "read_only" : "review" });
    } else {
      setActiveWorkflow({ scopeKey: currentScopeKey, focus });
    }
  };
  const openOrder = async (orderId: string) => {
    if (!isAdmin || !projection || projection.actor_role !== "admin" || !currentScopeKey) return;
    if (projection.cycle_status === "completed" || close.cycle.data?.stored_status === "completed") {
      setOrderStatus({ scopeKey: currentScopeKey, message: "本月已完成，请先填写原因重新打开，再修改订单。", error: true });
      return;
    }
    const generation = ++orderRequestGeneration.current;
    const scopeKey = currentScopeKey;
    setSelectedOrder(null);
    setOrderStatus({ scopeKey, message: "正在读取订单资料…" });
    try {
      const response = await ordersApi.get(orderId);
      if (generation !== orderRequestGeneration.current || activeScope.current !== scopeKey) return;
      const order = response.data as OrderOut;
      if (order.order_id !== orderId || order.check_out_date?.slice(0, 7) !== month) {
        setOrderStatus({ scopeKey, message: "这笔订单不属于当前月结月份，请重新核对订单和月份。", error: true });
        return;
      }
      setSelectedOrder({ scopeKey, order });
      setOrderStatus(null);
    } catch (error) {
      if (generation === orderRequestGeneration.current && activeScope.current === scopeKey)
        setOrderStatus({ scopeKey, message: extractErrorMessage(error, "订单读取失败，请重新点击该订单重试。"), error: true });
    }
  };
  const confirmFields = (documentId: string) => { void openDocumentReview(documentId); };
  const deleteCurrentDocument = async (document: MonthlyCloseProjectedDocumentSummary): Promise<DocumentRemovalResult> => {
    const latestInbox = await close.inbox.refetch();
    const inboxItems = latestInbox.data ?? close.inbox.data ?? [];
    const inboxItem = inboxItems.find((item) =>
      item.document_id === document.document_id
      || Boolean(document.receipt_id && item.item_id === document.receipt_id),
    );
    await close.archiveDocument.mutateAsync(document.document_id);
    if (!inboxItem) {
      return {
        outcome: "retained_for_audit",
        message: `${document.filename ?? "这份文件"} 已从本月对账中移除；这是旧版记录，原件按审计规则保留。`,
      };
    }
    try {
      await close.permanentlyDeleteInbox.mutateAsync(inboxItem.item_id);
      return {
        outcome: "deleted",
        message: `${document.filename ?? "这份文件"} 已永久删除；以后需要核对时请重新上传。`,
      };
    } catch (error) {
      const code = (error as { response?: { data?: { detail?: { code?: string } } } } | null)?.response?.data?.detail?.code;
      if (code === "inbox_item_financially_referenced") {
        return {
          outcome: "retained_for_audit",
          message: `${document.filename ?? "这份文件"} 已从本月对账中移除，但它已经进入正式账目凭证，原件按审计规则保留，不能永久删除。`,
        };
      }
      const reason = extractErrorMessage(error, "原件永久删除尚未完成");
      return {
        outcome: "retained_for_audit",
        message: `${document.filename ?? "这份文件"} 已从本月对账中移除；${reason}。你可以在撤销记录中稍后重试永久删除。`,
      };
    }
  };
  const primaryAction = () => {
    if (
      projection
      && projection.can_advance_final_review
      && projection.final_review.state !== "verified"
      && (
        projection.final_review.state !== "not_started"
        || projection.final_close_blockers.length === 0
      )
    ) {
      openWorkflow("final_review");
      return;
    }
    const action = projection?.recommended_action;
    if (!action) return;
    const destination = resolveMonthlyCloseAction(projection, action);
    if (!isAdmin && ["workflow_step", "inbox_detail", "remediation_detail"].includes(destination.kind)) {
      setActionDetail({ kind: "invalid", message: "此项操作仅限管理员，请联系管理员处理。" });
      return;
    }
    if (destination.kind === "invalid") message.warning(destination.message);
    else if (destination.kind === "final_review") openWorkflow("final_review");
    else if (destination.kind === "reopen") setReopenOpen(true);
    else if (destination.kind === "upload_source") {
      setUploadSourceType(destination.sourceType);
      fileInput.current?.click();
    } else if (destination.kind === "source_review") {
      if (destination.documentIds.length === 1) void openDocumentReview(destination.documentIds[0], destination.mode);
      else setActionDetail(destination);
    } else if (destination.kind === "source_workflow") {
      setActionDetail(destination);
    } else if (destination.kind === "workflow_step" && destination.mode === "confirm" && isAdmin) {
      setActionDetail(destination);
    } else if (["inbox_detail", "workflow_step", "remediation_detail"].includes(destination.kind)) {
      if (destination.kind === "inbox_detail" && !(close.inbox.data ?? []).some((item) => item.item_id === destination.inboxId)) {
        setActionDetail({ kind: "invalid", message: "这份收件资料已不可用，请刷新本月资料后重试。" });
      } else setActionDetail(destination);
    }
  };

  const renderStepPanel = (step: MonthlyCloseStep | undefined) => {
    if (!isAdmin) return <div role="status"><Button disabled>只有管理员可以处理此步骤</Button><p>请联系管理员核对本月事项。</p></div>;
    if (!close.cycle.data || close.cycle.data.cycle_id !== projection?.cycle_id || close.cycle.data.billing_month !== month) {
      return <div role="status">正在读取本月工作步骤…<Button onClick={() => close.cycle.refetch()}>重新读取步骤</Button></div>;
    }
    if (!step) return <div role="status"><Button disabled>此工作步骤暂不可用</Button><p>本月没有返回该步骤，请刷新后重试。</p><Button onClick={() => close.cycle.refetch()}>刷新步骤</Button></div>;
    return (<CurrentStepPanel
                      cycle={close.cycle.data}
                      step={step}
                      confirming={close.confirmStep.isPending}
                      uploading={close.uploadDocument.isPending || close.receiveInbox.isPending || close.classifyInbox.isPending || close.setInboxSource.isPending || close.confirmInbox.isPending || close.permanentlyDeleteInbox.isPending}
                      engineRunning={close.runUtility.isPending || close.importOperatingExpenses.isPending || close.reconcileServiceFees.isPending || close.generateSettlements.isPending}
                      readOnly={privacyMode || !isAdmin || close.cycle.data.stored_status === "completed"}
                      inboxItems={close.inbox.data ?? []}
                      onSelectStep={(key) => { setSelectedStepKey(key); if (renderedWorkflow) openWorkflow(key); }}
                      onConfirm={async () => {
                        if (!step) return;
                        await close.confirmStep.mutateAsync({ stepKey: step.step_key, evidenceHash: step.evidence_hash });
                        setSelectedStepKey(null);
                      }}
                      onReceiveInbox={async (file) => (await close.receiveInbox.mutateAsync(file)).data}
                      onClassifyInbox={async (itemId) => { await close.classifyInbox.mutateAsync(itemId); }}
                      onSetInboxSource={async (itemId, sourceType) => { await close.setInboxSource.mutateAsync({ itemId, sourceType }); }}
                      onConfirmInbox={async (itemId) => { await close.confirmInbox.mutateAsync(itemId); }}
                      onPermanentDeleteInbox={async (itemId) => { await close.permanentlyDeleteInbox.mutateAsync(itemId); }}
                      onUpload={(sourceType, file) => close.uploadDocument.mutateAsync({ sourceType, file })}
                      onNotApplicable={(sourceType, reason) => close.markNotApplicable.mutateAsync({ sourceType, reason })}
                      onArchive={(documentId) => close.archiveDocument.mutateAsync(documentId)}
                      onImportOperatingExpenses={async () => { await close.importOperatingExpenses.mutateAsync(); }}
                      onReconcileServiceFees={async () => { await close.reconcileServiceFees.mutateAsync(); }}
                      onGenerateSettlements={async () => { await close.generateSettlements.mutateAsync(); }}
                      onRefresh={() => close.refresh()}
                    />);
  };

  return (
    <div className="monthly-close-page" style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title={assistantEnabled ? "月结助理" : "月结中心"}
        subtitle={assistantEnabled
          ? undefined
          : manualMonthStatus(month, close.cycle.data, workflowStep)}
        extra={(
          <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 10 }}>
            {isAdmin && <Button disabled={privacyMode} onClick={() => setManagementOpen(true)}>管理员工具</Button>}
            <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ color: tokens.color.text.secondary }}>月份</span>
              <input aria-label="月结月份" type="month" value={month} onChange={(event) => selectMonth(event.target.value)} style={{ minHeight: 44, border: `1px solid ${tokens.anyu.color.linen}`, borderRadius: 8, background: tokens.anyu.color.shell, padding: "0 12px", font: "inherit", fontSize: 16 }} />
            </label>
          </div>
        )}
      />

      {close.projection.isLoading && !projection ? (
        <section
          role="status"
          aria-live="polite"
          aria-busy="true"
          style={{ maxWidth: 920, width: "100%", margin: "0 auto", border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, padding: 24, background: tokens.anyu.color.shell }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span aria-hidden="true" style={{ width: 36, height: 36, borderRadius: 999, display: "grid", placeItems: "center", background: tokens.anyu.color.ink.default, color: tokens.anyu.color.shell }}>月</span>
            <div>
              <div style={{ fontSize: 18, fontWeight: 500 }}>正在读取本月资料</div>
              <div style={{ marginTop: 4, color: tokens.anyu.color.stone }}>正在确认已上传文件、识别进度和下一步。</div>
            </div>
          </div>
          <div aria-hidden="true" style={{ marginTop: 22 }}><Skeleton active title={false} paragraph={{ rows: 3 }} /></div>
        </section>
      ) : close.projection.isError || !projection ? (
        statusOf(close.projection.error) === 404 && isAdmin && close.cycle.data === null ? (
          <Empty description="该月份尚未开始月结"><Button disabled={privacyMode} type="primary" loading={close.start.isPending} onClick={async () => { await close.start.mutateAsync(); await close.projection.refetch(); }}>开始该月月结</Button></Empty>
        ) : (
          <Alert role="alert" type="warning" showIcon message="当前月结内容不可用" description="你的权限可能已变化，或该月份暂时无法读取。请重新加载。" action={<Button onClick={() => close.projection.refetch()}>重新加载</Button>} />
        )
      ) : !assistantEnabled ? (
        isAdmin ? (
          close.cycle.isLoading ? <Skeleton active paragraph={{ rows: 8 }} />
            : close.cycle.isError || close.cycle.data === undefined ? (
              <Alert type="error" showIcon message="月结数据加载失败" description={extractErrorMessage(close.cycle.error, "请检查网络后重试")} action={<Button onClick={() => close.cycle.refetch()}>重新加载</Button>} />
            ) : close.cycle.data === null ? (
              <Empty description="该月份尚未开始月结"><Button disabled={privacyMode} type="primary" loading={close.start.isPending} onClick={async () => { await close.start.mutateAsync(); }}>开始该月月结</Button></Empty>
            ) : (
              <>
                {close.cycle.data.stored_status === "completed" && (
                  <Alert type={close.cycle.data.status === "needs_recheck" ? "warning" : "info"} showIcon message={close.cycle.data.status === "needs_recheck" ? "完成后的业务数据发生变化" : "该月份已完成，当前为只读状态"} description="如需重新处理，必须先填写原因重新打开；原完成快照和确认历史会保留。" action={<Button disabled={privacyMode} onClick={() => setReopenOpen(true)}>填写原因后重新打开</Button>} />
                )}
                <div className="monthly-close-workspace" style={{ display: "grid", gridTemplateColumns: "minmax(260px, 340px) minmax(0, 1fr)", gap: 24, alignItems: "start" }}>
                  <aside className="monthly-close-steps-shell" style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, overflow: "hidden" }}>
                    <CloseProgress steps={close.cycle.data.steps} currentStepKey={close.cycle.data.current_step} selectedStepKey={displayedStep?.step_key} onSelect={setSelectedStepKey} />
                  </aside>
                  <main className="monthly-close-current-panel" style={{ minWidth: 0, border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, padding: 24 }}>
                    {renderStepPanel(displayedStep)}
                  </main>
                </div>
              </>
            )
        ) : (
          <Alert role="alert" type="info" showIcon message="月结助理暂未开放" description="请把资料交给管理员，由管理员继续使用手工月结流程。" />
        )
      ) : (
        <MonthlyCloseWorkspace
          taskEnabled={isAdmin && !privacyMode}
          onOpenTaskStep={isAdmin && !privacyMode ? (stepKey) => {
            const target = chatWorkflowTarget(projection, stepKey);
            if (target.reason || !target.stepKey) return;
            reviewRequestGeneration.current += 1;
            setSelectedReview(null);
            setActionDetail(null);
            setActiveWorkflow({ scopeKey: currentScopeKey, focus: stepKey });
          } : undefined}
          projection={projection}
          assistantBusy={close.sendMessage.isPending}
          assistantProgress={close.assistantProgress}
          onStopMessage={close.stopMessage}
          onOpenWorkflow={!privacyMode && isAdmin ? openWorkflow : undefined}
          onOpenOrder={isAdmin ? (orderId) => { if (privacyMode) router.push(`/orders?order_id=${encodeURIComponent(orderId)}`); else void openOrder(orderId); } : undefined}
          inlineReview={!privacyMode && (actionDetail != null || renderedReview != null || renderedWorkflow != null) ? <section aria-label="聊天中的资料核对" style={{ marginTop: 24, minWidth: 0, borderTop: "1px solid var(--linen)", paddingTop: 16 }}>
            <Button onClick={() => { reviewRequestGeneration.current += 1; setActionDetail(null); setSelectedReview(null); setActiveWorkflow(null); }}>收起处理区</Button>
        {renderedWorkflow?.focus === "final_review" && projection ? <FinishMonthReview
          key={currentScopeKey} projection={projection} steps={close.cycle.data?.steps ?? []}
          onFinished={close.refresh} onOpenStep={openWorkflow} onClose={() => setActiveWorkflow(null)}
        /> : renderedWorkflow ? renderStepPanel(close.cycle.data?.steps.find((item) => item.step_key === chatWorkflowTarget(projection, renderedWorkflow.focus).stepKey)) : null}
        {renderedReview ? <DocumentReview key={`${month}-${renderedReview.detail.document_id}`} billingMonth={month} review={renderedReview} role={role} onFinished={async () => { await Promise.all([close.projection.refetch(), close.sourceProposals.refetch()]); }} /> : null}
        {actionDetail?.kind === "source_workflow" && actionDetail.sourceType === "system_service_fees" ? (
          <SystemServiceFeeReview
            key={`${currentScopeKey}:${actionDetail.sourceId}`}
            billingMonth={month}
            role={role}
            onFinished={async () => {
              await Promise.all([close.projection.refetch(), close.sourceProposals.refetch()]);
            }}
          />
        ) : actionDetail?.kind === "source_workflow" ? <div role="status"><Button disabled>该来源没有可用的处理入口</Button><p>请刷新本月资料，核对该来源是否仍需要处理。</p><Button onClick={() => close.refresh()}>刷新本月资料</Button></div> : null}
        {actionDetail?.kind === "source_review" ? (
          <div style={{ display: "grid", gap: 12 }}>
            <p>请选择服务端当前来源中的具体资料。系统不会自动代选第一份。</p>
            {actionDetail.documentIds.length ? actionDetail.documentIds.map((documentId) => (
              <Button key={documentId} disabled={!documentById(documentId)} onClick={() => { void openDocumentReview(documentId, actionDetail.mode, false); }}>{documentById(documentId)?.filename ?? "该资料已不可用，请刷新本月状态"}</Button>
            )) : <div><p>该来源还没有文件，请先上传本月资料或明确本月不适用的原因。</p>{renderStepPanel(close.cycle.data?.steps.find((item) => item.step_key === "source_collection"))}</div>}
          </div>
        ) : actionDetail?.kind === "workflow_step" ? renderStepPanel(close.cycle.data?.steps.find((item) => item.step_key === actionDetail.stepKey))
          : actionDetail?.kind === "inbox_detail" ? <SmartUploadInbox items={(close.inbox.data ?? []).filter((item) => item.item_id === actionDetail.inboxId)} showUploader={false} onReceive={async (file) => (await close.receiveInbox.mutateAsync(file)).data} onClassify={async (id) => { await close.classifyInbox.mutateAsync(id); }} onSetSource={async (itemId, sourceType) => { await close.setInboxSource.mutateAsync({ itemId, sourceType }); }} onConfirm={async (id) => { await close.confirmInbox.mutateAsync(id); }} />
          : actionDetail && actionDetail.kind !== "source_workflow" ? <div role="status"><Button disabled>当前事项没有可用的处理入口</Button><p>{actionDetail.kind === "invalid" ? actionDetail.message : "请刷新本月状态，核对该事项是否仍存在。"}</p><Button onClick={() => close.refresh()}>刷新本月状态</Button></div> : null}
          </section> : null}
          scopeKey={currentScopeKey}
          events={close.events}
          replies={close.replies}
          receipts={close.durableReceipts}
          inboxItems={close.inbox.data ?? []}
          onPrimaryAction={primaryAction}
          onSendMessage={async (text, attachmentIds, contextRunId) => (await close.sendMessage.mutateAsync({ text, attachmentIds, contextRunId })).data}
          onReceiveCaseFile={isAdmin ? async (file, onProgress) => (await monthlyCloseApi.receiveFinancialCaseFile(month, file, onProgress)).data : undefined}
          onReceiveInbox={async (file) => (await close.receiveInbox.mutateAsync(file)).data}
          onClassifyInbox={async (itemId) => { await close.classifyInbox.mutateAsync(itemId); }}
          onSetInboxSource={async (itemId, sourceType) => { await close.setInboxSource.mutateAsync({ itemId, sourceType }); }}
          onConfirmInbox={async (itemId) => { await close.confirmInbox.mutateAsync(itemId); }}
          onPermanentDeleteInbox={isAdmin ? async (itemId) => { await close.permanentlyDeleteInbox.mutateAsync(itemId); } : undefined}
          onMarkNotApplicable={isAdmin ? async (sourceType, reason) => { await close.markNotApplicable.mutateAsync({ sourceType, reason }); } : undefined}
          onDeleteDocument={isAdmin ? deleteCurrentDocument : undefined}
          onRetryAnalysis={(documentId) => {
            const document = documentById(documentId);
            if (!document) return;
            void close.retryAnalysis.mutateAsync(document).then(() => message.success("已重新开始分析")).catch((error) => message.error(extractErrorMessage(error, "重新分析失败")));
          }}
          onConfirmFields={confirmFields}
          onReviewDocument={(documentId) => { void openDocumentReview(documentId); }}
          onRequestEvidence={(documentId) => close.loadEvidence(documentId)}
        />
      )}

      {isAdmin && orderStatus?.scopeKey === currentScopeKey && <div role={orderStatus.error ? "alert" : "status"}>{orderStatus.message}</div>}
      {!privacyMode && renderedOrder && <EditOrderModal key={`${renderedOrder.scopeKey}:${renderedOrder.order.order_id}`} open order={renderedOrder.order} onClose={() => {
        const scopeKey = renderedOrder.scopeKey;
        setSelectedOrder(null);
        if (activeScope.current === scopeKey) void close.refresh();
      }} />}

      <input ref={fileInput} type="file" hidden accept=".xls,.xlsx" onChange={(event) => {
        const file = event.target.files?.[0];
        event.target.value = "";
        if (!file) return;
        const sourceType = uploadSourceType;
        setUploadSourceType(null);
        if (!sourceType) return;
        void close.uploadDocument.mutateAsync({ sourceType, file }).then(() => message.success("文件已保存，后台会继续处理")).catch((error) => message.error(extractErrorMessage(error, "文件保存失败")));
      }} />



      <Modal title="重新打开本月月结" open={reopenOpen} okText="重新打开" cancelText="取消" confirmLoading={close.reopen.isPending} okButtonProps={{ disabled: privacyMode || !reopenReason.trim() }} onCancel={() => { setReopenOpen(false); setReopenReason(""); }} onOk={async () => { if (privacyMode) return; await close.reopen.mutateAsync(reopenReason.trim()); await close.projection.refetch(); setReopenOpen(false); setReopenReason(""); }}>
        <Input.TextArea aria-label="重新打开原因" rows={4} maxLength={1000} value={reopenReason} onChange={(event) => setReopenReason(event.target.value)} />
      </Modal>

      {isAdmin && (
        <Drawer title="管理员工具" width={760} open={managementOpen} destroyOnHidden onClose={() => { setManagementOpen(false); setAdminAdvancedOpen(false); }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
            <section aria-label="业主结算入口" style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, padding: 16 }}>
              <h2 style={{ fontSize: 18, fontWeight: 500, margin: 0 }}>业主结算</h2>
              <p style={{ color: tokens.anyu.color.stone, lineHeight: 1.7, margin: "6px 0 14px" }}>查看、生成和复核业主结算单。这里与本月资料核对是两个独立流程。</p>
              <Button onClick={() => router.push("/settlements")}>打开业主结算</Button>
            </section>
            {close.overview.isError ? <Alert type="warning" showIcon message="月份总览暂时加载失败" /> : <MonthlyCloseControlTower rows={close.overview.data ?? []} selectedMonth={month} onSelect={(next) => { selectMonth(next); setManagementOpen(false); }} />}
            <section style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, padding: "0 16px" }}>
              <button
                type="button"
                aria-expanded={adminAdvancedOpen}
                onClick={() => setAdminAdvancedOpen((current) => !current)}
                style={{ width: "100%", minHeight: 52, display: "flex", alignItems: "center", border: 0, padding: 0, background: "transparent", color: tokens.anyu.color.ink.default, font: "inherit", fontWeight: 500, cursor: "pointer" }}
              >
                高级诊断与收件设置
              </button>
              {adminAdvancedOpen && <div style={{ display: "flex", flexDirection: "column", gap: 28, padding: "4px 0 18px" }}>
                <Alert type="info" showIcon message="这些是管理员排查工具" description="普通对账不需要使用。只有识别失败、供应商外部收件或读取规则异常时再展开处理。" />
                <section aria-label="月结九步诊断区">
                  <h2 style={{ fontSize: 18, fontWeight: 500, margin: "0 0 10px" }}>月结九步诊断（只读）</h2>
                  <nav aria-label="月结九步诊断">
                    <ol style={{ margin: 0, paddingLeft: 24 }}>
                      {(close.cycle.data?.steps ?? []).map((step) => <li key={step.step_key} style={{ padding: "7px 0", color: tokens.anyu.color.stone }}>{step.label} · {step.status === "confirmed" ? "已确认" : step.blocking_count ? `待处理 ${step.blocking_count} 项` : "只读查看"}</li>)}
                    </ol>
                  </nav>
                </section>
                <SmartUploadInbox
                  items={close.inbox.data ?? []}
                  disabled={close.cycle.data?.stored_status === "completed"}
                  onReceive={async (file) => (await close.receiveInbox.mutateAsync(file)).data}
                  onClassify={async (itemId) => { await close.classifyInbox.mutateAsync(itemId); }}
                  onSetSource={async (itemId, sourceType) => { await close.setInboxSource.mutateAsync({ itemId, sourceType }); }}
                  onConfirm={async (itemId) => { await close.confirmInbox.mutateAsync(itemId); }}
                  onPermanentDelete={async (itemId) => { await close.permanentlyDeleteInbox.mutateAsync(itemId); }}
                />
                <section aria-label="供应商收件链接管理">
                  <h2 style={{ fontSize: 18, fontWeight: 500 }}>供应商收件链接</h2>
                  <IntakeLinkManager creationEnabled={projection?.features.external_intake_enabled === true} links={close.intakeLinks.data ?? []} onCreate={async (label, sourceType) => (await close.createIntakeLink.mutateAsync({ label, sourceType })).data} onRevoke={async (linkId) => { await close.revokeIntakeLink.mutateAsync(linkId); }} />
                </section>
                <section aria-label="表格布局记忆管理">
                  <h2 style={{ fontSize: 18, fontWeight: 500 }}>表格布局记忆</h2>
                  <LayoutMemoryManager memories={close.layoutMemories.data ?? []} metrics={close.layoutMetrics.data ?? EMPTY_LAYOUT_METRICS} onSetEnabled={async (documentId, value) => { await close.setLayoutMemoryEnabled.mutateAsync({ documentId, value }); }} />
                </section>
              </div>}
            </section>
          </div>
        </Drawer>
      )}
    </div>
  );
}

function DocumentReview({ billingMonth, review, role, onFinished }: {
  billingMonth: string;
  review: { sourceType: string; mode: "review" | "read_only"; detail: MonthlyCloseProjectedDocument };
  role: string;
  onFinished: () => Promise<unknown>;
}) {
  const document: MonthlyCloseDocument = {
    document_id: review.detail.document_id,
    filename: review.detail.filename ?? "月结资料",
    byte_size: review.detail.byte_size ?? 0,
    processing_status: review.detail.storage_state === "rejected" ? "rejected" : "stored",
    processing_error: review.detail.analysis_error ?? null,
    engine_type: review.detail.engine_type ?? null,
    engine_id: review.detail.engine_id ?? null,
    uploaded_at: review.detail.uploaded_at ?? null,
  };
  if (review.mode === "read_only") {
    return <div><EvidencePanel selected={review.detail} loading={false} error={false} /><Button disabled>当前资料仅可查看</Button><p>请先重新打开月份或解除当前阻塞，再继续处理资料。</p></div>;
  }
  if (review.sourceType === "ota_statement") return <OtaStatementPanel billingMonth={billingMonth} documents={[document]} role={role} onFinished={onFinished} />;
  if (["cleaning_statement", "linen_statement"].includes(review.sourceType)) return <ServiceMappingReview embedded billingMonth={billingMonth} document={document} sourceType={review.sourceType} role={role} onFinished={onFinished} />;
  if (["utility_receipt", "utility_expense"].includes(review.sourceType)) return <UtilityMappingReview billingMonth={billingMonth} document={document} sourceType={review.sourceType} role={role} onFinished={onFinished} />;
  if (review.sourceType === "operating_expenses") return <OperatingExpenseMappingReview billingMonth={billingMonth} document={document} role={role} onFinished={onFinished} />;
  return <Alert type="info" showIcon message={`只读查看：${document.filename}`} description="该资料暂时没有可用的写入入口，请联系管理员继续处理。" />;
}
