"use client";

import { type ClipboardEvent, type DragEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { ArrowUpOutlined, FileExcelOutlined, FileImageOutlined, PaperClipOutlined } from "@ant-design/icons";
import { Drawer, Modal } from "antd";

import type {
  AssistantReply,
  FinancialCaseUploadReceipt,
  MonthlyCloseDurableReceipt,
  MonthlyCloseEvent,
  MonthlyCloseInboxItem,
  MonthlyCloseProjectedDocument,
  MonthlyCloseProjectedDocumentSummary,
  MonthlyCloseProjection,
  MonthlyCloseProjectionSource,
  MonthlyCloseReceiptView,
} from "@/lib/monthly-close";
import type { AssistantProgress } from "@/lib/monthly-close-chat-stream";
import { extractErrorMessage } from "@/lib/api-errors";
import { tokens } from "@/lib/design-tokens";
import { usePrivacyMode } from "@/hooks/usePrivacyMode";
import { EvidencePanel } from "./EvidencePanel";
import { SmartUploadInbox } from "./SmartUploadInbox";
import { SourceChecklist, sourceLabel } from "./SourceChecklist";
import { chatWorkflowTarget } from "./ChatWorkflowActions";
import { WorkTimeline } from "./WorkTimeline";
import { MonthlyCloseTaskProposal } from "./MonthlyCloseTaskProposal";
import { MonthlyCloseTaskProgress } from "./MonthlyCloseTaskProgress";
import { MonthOutcome } from "./MonthOutcome";
import { monthlyFiles, evidenceFilename, sourceReceiptState } from "@/lib/monthly-close-experience";
import { useMonthlyCloseTask } from "@/hooks/useMonthlyCloseTask";

export interface MonthlyCloseWorkspaceProps {
  projection: MonthlyCloseProjection;
  inlineReview?: ReactNode;
  taskEnabled?: boolean;
  onOpenTaskStep?: (stepKey: string) => void;
  assistantBusy?: boolean;
  assistantProgress?: AssistantProgress | null;
  onStopMessage?: () => void;
  events: MonthlyCloseEvent[];
  replies: AssistantReply[];
  receipts?: MonthlyCloseReceiptView[];
  inboxItems?: MonthlyCloseInboxItem[];
  scopeKey?: string;
  onPrimaryAction: () => void;
  onSendMessage: (text: string, attachmentIds: string[], contextRunId?: string) => void | AssistantReply | Promise<void | AssistantReply>;
  onRetryAnalysis: (documentId: string) => void;
  onConfirmFields: (documentId: string) => void;
  onReviewDocument?: (documentId: string) => void;
  onOpenWorkflow?: (focus: string) => void;
  onOpenOrder?: (orderId: string) => void;
  onDeleteDocument?: (document: MonthlyCloseProjectedDocumentSummary) => Promise<DocumentRemovalResult>;
  onRequestEvidence: (documentId: string) => void | MonthlyCloseProjectedDocument | Promise<void | MonthlyCloseProjectedDocument>;
  onReceiveCaseFile?: (file: File, onProgress?: (percent: number) => void) => Promise<FinancialCaseUploadReceipt>;
  onReceiveInbox?: (file: File) => Promise<MonthlyCloseDurableReceipt>;
  onClassifyInbox?: (itemId: string) => Promise<unknown>;
  onSetInboxSource?: (itemId: string, sourceType: string) => Promise<unknown>;
  onConfirmInbox?: (itemId: string) => Promise<unknown>;
  onPermanentDeleteInbox?: (itemId: string) => Promise<unknown>;
  onMarkNotApplicable?: (sourceType: string, reason: string) => Promise<unknown>;
}

export interface DocumentRemovalResult {
  outcome: "deleted" | "retained_for_audit";
  message: string;
}

const touchButton = { minHeight: 44, borderRadius: 999, padding: "0 18px", font: "inherit" } as const;
const noopAsync = async () => undefined;
type PendingFile = { attachmentId: string; filename: string; image?: boolean; detail?: string };

function monthLabel(month: string) {
  const [year, value] = month.split("-");
  return `${year}年${value}月`;
}

function isWorkbook(file: File) {
  return /\.xlsx?$/i.test(file.name);
}

export function MonthlyCloseWorkspace({
  projection,
  inlineReview,
  taskEnabled = false,
  onOpenTaskStep,
  assistantBusy = false,
  assistantProgress,
  onStopMessage,
  events,
  replies,
  receipts = [],
  inboxItems = [],
  scopeKey = `${projection.cycle_id}:${projection.actor_role}`,
  onPrimaryAction,
  onSendMessage,
  onRetryAnalysis,
  onConfirmFields,
  onReviewDocument,
  onOpenWorkflow,
  onOpenOrder,
  onDeleteDocument,
  onRequestEvidence,
  onReceiveInbox,
  onReceiveCaseFile,
  onClassifyInbox = noopAsync,
  onSetInboxSource = noopAsync,
  onConfirmInbox = noopAsync,
  onPermanentDeleteInbox,
  onMarkNotApplicable,
}: MonthlyCloseWorkspaceProps) {
  const privacyMode = usePrivacyMode();
  const [taskProposal, setTaskProposal] = useState<{ scope: string; month: string; id: string } | null>(null);
  const receiveComposerFile = onReceiveCaseFile ?? onReceiveInbox;
  const [uploadProgress, setUploadProgress] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const composingMessage = useRef(false);
  const conversationRef = useRef<HTMLElement>(null);
  const followLatest = useRef(true);
  const manualScroll = useRef(false);
  const [newReplies, setNewReplies] = useState(false);
  useEffect(() => {
    const conversation = conversationRef.current;
    if (!conversation) return;
    if (followLatest.current) conversation.scrollTop = conversation.scrollHeight;
    else setNewReplies(true);
  }, [replies.length, assistantBusy]);
  const [message, setMessage] = useState("");
  const [uploadingCount, setUploadingCount] = useState(0);
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [composerError, setComposerError] = useState<string | null>(null);
  const [localSending, setSending] = useState(false);
  const taskAvailable = taskEnabled && projection.actor_role === "admin" && !privacyMode;
  const taskRefreshKey = `${replies.length}:${events.length}:${pendingFiles.length}:${receipts.length}:${projection.input_hash}`;
  const monthlyTask = useMonthlyCloseTask(projection.billing_month, scopeKey, taskAvailable, taskRefreshKey);
  const sending = localSending || assistantBusy || monthlyTask.pending;
  const sendingRequest = useRef(false);
  const uploadingRequest = useRef(false);
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);
  const [waitingLong, setWaitingLong] = useState(false);
  const [exchanges, setExchanges] = useState<Array<{ id: string; userText: string; assistantReply?: AssistantReply }>>([]);
  const [dragActive, setDragActive] = useState(false);
  const [progressOpen, setProgressOpen] = useState(false);
  const [selectedEvidence, setSelectedEvidence] = useState<{ detail: MonthlyCloseProjectedDocument; scopeKey: string } | null>(null);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [evidenceError, setEvidenceError] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<MonthlyCloseProjectedDocumentSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteNotice, setDeleteNotice] = useState<string | null>(null);
  const [notApplicableTarget, setNotApplicableTarget] = useState<MonthlyCloseProjectionSource | null>(null);
  const [notApplicableReason, setNotApplicableReason] = useState("");
  const [markingNotApplicable, setMarkingNotApplicable] = useState(false);
  const [notApplicableError, setNotApplicableError] = useState<string | null>(null);
  const evidenceRequest = useRef(0);
  const uploadRequest = useRef(0);
  const visibleDocumentIds = projection.sources.flatMap((source) => source.documents.map((document) => document.document_id));
  const visibleDocumentKey = visibleDocumentIds.join(":");
  const renderedEvidence = selectedEvidence
    && selectedEvidence.scopeKey === scopeKey
    && visibleDocumentIds.includes(selectedEvidence.detail.document_id)
    ? selectedEvidence.detail
    : null;

  useEffect(() => {
    evidenceRequest.current += 1;
    setSelectedEvidence(null);
    setEvidenceLoading(false);
    setEvidenceError(false);
    setProgressOpen(false);
    setDeleteTarget(null);
    setDeleting(false);
    setDeleteError(null);
    setDeleteNotice(null);
    setNotApplicableTarget(null);
    setNotApplicableReason("");
    setMarkingNotApplicable(false);
    setNotApplicableError(null);
  }, [scopeKey, visibleDocumentKey]);

  useEffect(() => {
    uploadRequest.current += 1;
    setMessage("");
    setPendingFiles([]);
    setComposerError(null);
    setUploadingCount(0);
    setUploadProgress(null);
    setDragActive(false);
    setExchanges([]);
    setSending(false);
    sendingRequest.current = false;
    uploadingRequest.current = false;
    setPendingMessage(null);
    setWaitingLong(false);
    followLatest.current = true;
    manualScroll.current = false;
    setNewReplies(false);
    return () => { uploadRequest.current += 1; };
  }, [scopeKey]);

  useEffect(() => {
    if (!sending) return;
    const timer = window.setTimeout(() => setWaitingLong(true), 10_000);
    return () => window.clearTimeout(timer);
  }, [sending, scopeKey]);

  const allFiles = monthlyFiles(projection, receipts, inboxItems);
  const pendingAttachmentIds = new Set(pendingFiles.map((file) => file.attachmentId));
  const inboxReceiptIds = new Set(inboxItems.map((item) => item.item_id));
  const actionableInboxItems = inboxItems.filter((item) => item.status !== "confirmed" && !pendingAttachmentIds.has(item.item_id));
  const timelineReceipts = receipts.filter((receipt) => !pendingAttachmentIds.has(receipt.receipt_id) && !inboxReceiptIds.has(receipt.receipt_id));
  const action = projection.recommended_action;
  const finalizationVerified = projection.final_review.state === "verified";
  const showFinalReview = ["admin", "finance"].includes(projection.actor_role)
    && !finalizationVerified
    && projection.final_close_blockers.length === 0;
  const actionIsPassive = showFinalReview || ["none", "wait", "wait_for_review", "wait_for_assignment", "escalate", "start_final_review"].includes(action.kind);
  const actionIsGenericUpload = action.kind === "provide_source";
  const canDeleteDocuments = !privacyMode && projection.actor_role === "admin" && !showFinalReview && Boolean(onDeleteDocument);
  const actionTargetSource = projection.sources.find((source) => source.source_id === action.target_id || source.source_type === action.target_id);
  const actionTargetDocument = projection.sources
    .flatMap((source) => source.documents)
    .find((document) => document.document_id === action.target_id)
    ?? (actionTargetSource?.documents.length === 1 ? actionTargetSource.documents[0] : undefined);
  const actionLabel = actionTargetDocument?.work_record_count ? `查看《${actionTargetDocument.filename}》的补齐结果` : action.kind === "confirm_analysis" && actionTargetDocument?.filename
    ? `确认《${actionTargetDocument.filename}》的识别结果`
    : action.label;
  const actionLivesWithDocument = !actionIsPassive && !actionIsGenericUpload && Boolean(actionTargetDocument);

  const requestDelete = (document: MonthlyCloseProjectedDocumentSummary) => {
    if (!canDeleteDocuments) return;
    setDeleteError(null);
    setDeleteTarget(document);
  };

  const confirmDelete = async () => {
    if (!deleteTarget || !onDeleteDocument || deleting) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      const result = await onDeleteDocument(deleteTarget);
      setDeleteNotice(result.message);
      if (selectedEvidence?.detail.document_id === deleteTarget.document_id) {
        evidenceRequest.current += 1;
        setSelectedEvidence(null);
        setEvidenceLoading(false);
        setEvidenceError(false);
      }
      setDeleteTarget(null);
      setProgressOpen(false);
    } catch (error) {
      setDeleteError(extractErrorMessage(error, "文件没有删除，请刷新后重试"));
    } finally {
      setDeleting(false);
    }
  };

  const confirmNotApplicable = async () => {
    const reason = notApplicableReason.trim();
    if (!notApplicableTarget || !onMarkNotApplicable || !reason || markingNotApplicable) return;
    setMarkingNotApplicable(true);
    setNotApplicableError(null);
    try {
      await onMarkNotApplicable(notApplicableTarget.source_type, reason);
      setNotApplicableTarget(null);
      setNotApplicableReason("");
    } catch (error) {
      setNotApplicableError(extractErrorMessage(error, "暂时无法保存，请重试"));
    } finally {
      setMarkingNotApplicable(false);
    }
  };

  const selectDocument = async (document: MonthlyCloseProjectedDocumentSummary) => {
    const requestId = ++evidenceRequest.current;
    setEvidenceError(false);
    setEvidenceLoading(true);
    setProgressOpen(true);
    try {
      const detail = await onRequestEvidence(document.document_id);
      if (requestId === evidenceRequest.current) setSelectedEvidence({ detail: detail || document, scopeKey });
    } catch {
      if (requestId === evidenceRequest.current) {
        setSelectedEvidence(null);
        setEvidenceError(true);
      }
    } finally {
      if (requestId === evidenceRequest.current) setEvidenceLoading(false);
    }
  };

  const receiveFiles = async (files: File[]) => {
    if (privacyMode || !receiveComposerFile || files.length === 0 || sendingRequest.current || uploadingRequest.current) return;
    const attachmentLimit = onReceiveCaseFile ? 20 : 10;
    if (pendingFiles.length + files.length > attachmentLimit) {
      setComposerError(`每次最多${attachmentLimit}份，请先发送这一批再继续。此次选择的 ${files.length} 份文件均未上传，请分批添加。`);
      return;
    }
    const acceptedFiles = files.filter((file) => isWorkbook(file) || Boolean(onReceiveCaseFile && /\.(docx|png|jpe?g)$/i.test(file.name)));
    if (acceptedFiles.length !== files.length) {
      setComposerError(onReceiveCaseFile ? "支持 Excel、Word 核实清单（.docx）和截图（.png / .jpg / .jpeg），其他文件未接收。" : "这里只能发送 Excel 文件（.xls 或 .xlsx）");
    } else {
      setComposerError(null);
    }
    if (acceptedFiles.length === 0) return;
    const generation = uploadRequest.current;
    uploadingRequest.current = true;
    setUploadingCount(acceptedFiles.length);
    for (const file of acceptedFiles) {
      if (generation !== uploadRequest.current) break;
      try {
        setUploadProgress(`正在传输 ${file.name}`);
        const reportProgress = (percent: number) => {
          if (generation === uploadRequest.current) setUploadProgress(percent < 100 ? `正在传输 ${file.name} · ${percent}%` : `${file.name} 已传输，正在校验资料`);
        };
        const receipt = onReceiveCaseFile ? await onReceiveCaseFile(file, reportProgress) : await receiveComposerFile(file);
        if (generation !== uploadRequest.current) continue;
        setPendingFiles((current) => current.some((item) => item.attachmentId === receipt.item_id)
          ? current
          : [...current, { attachmentId: receipt.item_id, filename: file.name, image: /\.(png|jpe?g)$/i.test(file.name),
            detail: "fact_count" in receipt ? `${receipt.kind === "feedback" ? "同事回填的核实依据" : receipt.kind === "image" || /\.(png|jpe?g)$/i.test(file.name) ? "图片文字待核实" : receipt.fact_count > 0 ? `${receipt.fact_count} 条来源明细` : "未识别到明细，请补充说明"}${receipt.issues_count ? ` · ${receipt.issues_count} 项待核对` : ""}` : undefined }]);
        if ("issues" in receipt && receipt.issues?.length) {
          setComposerError((current) => [current, ...receipt.issues!.map((issue) => `${file.name}：${issue.message}`)].filter(Boolean).join("\n"));
        }
      } catch (error) {
        if (generation === uploadRequest.current) {
          setComposerError((current) => [current, `${file.name}：${extractErrorMessage(error, "文件没有收到，请重试")}`].filter(Boolean).join("\n"));
        }
      } finally {
        if (generation === uploadRequest.current) {
          setUploadingCount((current) => Math.max(0, current - 1));
        }
      }
    }
    if (generation === uploadRequest.current) { uploadingRequest.current = false; setUploadProgress(null); }
  };

  const sendToAssistant = async () => {
    if (privacyMode) return;
    const text = message.trim();
    if ((!text && pendingFiles.length === 0) || sendingRequest.current || uploadingRequest.current || monthlyTask.pending) return;
    setComposerError(null);
    sendingRequest.current = true;
    setSending(true);
    setWaitingLong(false);
    setPendingMessage(text || "请查看我刚上传的月结资料");
    setMessage("");
    followLatest.current = true;
    manualScroll.current = false;
    setNewReplies(false);
    if (conversationRef.current) conversationRef.current.scrollTop = conversationRef.current.scrollHeight;
    const generation = uploadRequest.current;
    try {
      const reply = await onSendMessage(
        text || "请查看我刚上传的月结资料",
        pendingFiles.map((file) => file.attachmentId),
      );
      if (generation !== uploadRequest.current) return;
      if (text) {
        setExchanges((current) => [...current, {
          id: reply?.run_id ?? `local-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`,
          userText: text,
          assistantReply: reply || undefined,
        }]);
      }
      setPendingFiles([]);
      void monthlyTask.refresh();
    } catch (error) {
      if (generation === uploadRequest.current) {
        setComposerError(extractErrorMessage(error, "消息没有发送成功，请重试"));
        setMessage((draft) => draft || text);
      }
    } finally {
      if (generation === uploadRequest.current) {
        sendingRequest.current = false;
        setSending(false);
        setPendingMessage(null);
        setWaitingLong(false);
      }
    }
  };

  const followUpInvestigation = async (text: string, contextRunId?: string, attachmentIds: string[] = []) => {
    if (privacyMode) return;
    if (sendingRequest.current || uploadingRequest.current || monthlyTask.pending) return;
    const generation = uploadRequest.current;
    sendingRequest.current = true;
    setSending(true);
    setWaitingLong(false);
    setPendingMessage(text);
    setComposerError(null);
    try {
      const reply = await onSendMessage(text, attachmentIds, contextRunId);
      if (generation !== uploadRequest.current) return;
      if (reply) setExchanges((current) => [...current, { id: reply.run_id, userText: text, assistantReply: reply }]);
      void monthlyTask.refresh();
    } catch (error) {
      if (generation === uploadRequest.current) setComposerError(extractErrorMessage(error, "调查没有完成，请重试"));
    } finally {
      if (generation === uploadRequest.current) {
        sendingRequest.current = false;
        setSending(false);
        setPendingMessage(null);
        setWaitingLong(false);
      }
    }
  };

  const handleDrop = (event: DragEvent<HTMLFormElement>) => {
    event.preventDefault();
    setDragActive(false);
    if (!receiveComposerFile || uploadingCount > 0) return;
    void receiveFiles(Array.from(event.dataTransfer.files));
  };

  const reviewWorkLog = (documentId: string) => {
    setProgressOpen(false);
    void followUpInvestigation("核对保洁记录", undefined, [documentId]);
  };

  const handlePaste = (event: ClipboardEvent<HTMLFormElement>) => {
    const data = event.clipboardData;
    const items = Array.from(data.items ?? []).filter((item) => item.kind === "file");
    const files = data.files.length ? Array.from(data.files)
      : items.map((item) => item.getAsFile()).filter((file): file is File => file !== null);
    if (!files.length && !items.length && !Array.from(data.types ?? []).includes("Files")) return;
    event.preventDefault();
    if (!receiveComposerFile) { setComposerError("当前账号不能上传资料"); return; }
    if (sendingRequest.current || uploadingRequest.current) {
      setComposerError("当前操作还在处理中，请完成后再粘贴文件。");
      return;
    }
    if (!files.length) {
      setComposerError("没有读到文件内容，请把文件拖到这里，或点击回形针选择文件。");
      return;
    }
    void receiveFiles(files);
  };

  return (
    <section className="mcw-chat" role="region" aria-label="月结助理工作台" data-desktop-columns="single-chat">
      <header className="mcw-chat-header">
        <div className="mcw-assistant-identity">
          <span className="mcw-avatar" aria-hidden="true">月</span>
          <span>
            <strong>{monthLabel(projection.billing_month)}对账</strong>
            <small>{finalizationVerified ? "整月关账已完成" : projection.final_review.confirmed_settlement_count ? "业主结算已有确认 · 经营关账进行中" : "进行中"}</small>
          </span>
        </div>
        <div className="mcw-header-actions">
          <button type="button" className="mcw-progress-button" onClick={() => setProgressOpen(true)}>查看本月进度</button>
          {!privacyMode && projection.actor_role === "admin" && onOpenWorkflow && <button type="button"
            className="mcw-progress-button" disabled={sending || uploadingCount > 0}
            onClick={() => onOpenWorkflow("final_review")}>
            {finalizationVerified ? "查看结束结果" : projection.final_review.settlement_count > 0 && projection.final_review.confirmed_settlement_count === projection.final_review.settlement_count ? "查看结算与收尾" : "结束本月对账"}
          </button>}
        </div>
      </header>

      <MonthOutcome projection={projection} fileCount={allFiles.length} onFiles={() => setProgressOpen(true)}
        onFinish={!privacyMode && projection.actor_role === "admin" && onOpenWorkflow ? () => onOpenWorkflow("final_review") : undefined} />
      {privacyMode && <p>隐私演示模式：可查看进度和记录，暂不能发送、上传或修改资料。</p>}

      {taskAvailable && <MonthlyCloseTaskProgress
        key={`${scopeKey}:${projection.billing_month}`}
        month={projection.billing_month}
        ownerSettlementsConfirmed={projection.final_review.settlement_count > 0 && projection.final_review.confirmed_settlement_count === projection.final_review.settlement_count}
        task={monthlyTask.task}
        loading={monthlyTask.loading}
        pending={monthlyTask.pending}
        busy={sending || uploadingCount > 0}
        error={monthlyTask.error}
        onCommand={async (input) => {
          const generation = uploadRequest.current;
          const result = await monthlyTask.command(input);
          if (result && input.action === "start" && generation === uploadRequest.current) setPendingFiles([]);
        }}
        onAction={async (action) => {
          if (action.proposal_id) {
            setTaskProposal({ scope: scopeKey, month: projection.billing_month, id: action.proposal_id });
            return;
          }
          await followUpInvestigation(action.text, action.context_run_id, action.attachment_ids ?? []);
          await monthlyTask.refresh();
        }}
        questionAction={(question) => {
          const step = question.step_key?.replace(/^confirmation:/, "");
          if (step && onOpenTaskStep) {
            const target = chatWorkflowTarget(projection, step);
            if (!target.reason && target.stepKey) return { label: `查看${target.label}`, run: () => onOpenTaskStep(step) };
          }
          const source = projection.sources.find((item) => item.source_id === question.source_id);
          if (source && onOpenWorkflow) {
            const target = chatWorkflowTarget(projection, source.source_type);
            if (!target.reason) return { label: `查看${target.label}`, run: () => onOpenWorkflow(source.source_type) };
          }
          return null;
        }}
        onRefresh={monthlyTask.refresh}
        onAnswer={() => conversationRef.current?.parentElement?.querySelector("textarea")?.focus()}
      />}

      <main ref={conversationRef} className="mcw-conversation" aria-label="与月结助理的对话"
        onWheel={() => { manualScroll.current = true; }}
        onTouchMove={() => { manualScroll.current = true; }}
        onPointerDown={(event) => { if (event.target === event.currentTarget) manualScroll.current = true; }}
        onKeyDown={(event) => { if (["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown"].includes(event.key)) manualScroll.current = true; }}
        onScroll={() => {
        const node = conversationRef.current;
        if (!node) return;
        const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
        if (nearBottom) { followLatest.current = true; manualScroll.current = false; }
        else if (manualScroll.current) followLatest.current = false;
        if (followLatest.current) setNewReplies(false);
      }}>
        <div className="mcw-message-row mcw-message-assistant">
          <span className="mcw-avatar mcw-message-avatar" aria-hidden="true">月</span>
          <div className="mcw-bubble mcw-assistant-bubble">
            <div style={{ fontWeight: 500 }}>{onReceiveCaseFile ? "把原始表格和截图发到这里，一起核对" : "把你手头已有的表格直接发给我"}</div>
            <div style={{ marginTop: 6, color: tokens.anyu.color.stone, lineHeight: 1.7 }}>
              {onReceiveCaseFile ? `每批最多 20 份。先保存原件并核对；补充说明后，先确认来源解释，再确认具体记账方案。截图金额需逐项核实。当前工作区为 ${projection.billing_month}，可在同一聊天说明其他月份的费用，按方案逐笔确认；报告可按所选月份汇总。` : "不用先选资料类别。你可以补一句“这是8月保洁记录”，我会识别内容、开始核对，判断不准时再问你。"}
            </div>
          </div>
        </div>

        {!actionIsGenericUpload && !actionLivesWithDocument && (
          <div className="mcw-message-row mcw-message-assistant">
            <span className="mcw-avatar mcw-message-avatar" aria-hidden="true">月</span>
            <div className="mcw-bubble mcw-assistant-bubble">
              <div style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>建议你下一步</div>
              <div style={{ marginTop: 4 }}>{showFinalReview ? "最终复核进行中" : action.kind === "start_final_review" ? "资料与结算已就绪" : actionLabel}</div>
              {!privacyMode && !actionIsPassive && (
                <button type="button" className="mcw-primary-button" style={{ ...touchButton, marginTop: 12, color: tokens.anyu.color.shell, background: tokens.anyu.color.ink.default, border: `1px solid ${tokens.anyu.color.ink.default}` }} onClick={onPrimaryAction}>
                  {actionLabel}
                </button>
              )}
              {!privacyMode && showFinalReview && projection.can_advance_final_review && (
                <button type="button" className="mcw-primary-button" style={{ ...touchButton, marginTop: 12, color: tokens.anyu.color.shell, background: tokens.anyu.color.ink.default, border: `1px solid ${tokens.anyu.color.ink.default}` }} onClick={onPrimaryAction}>
                  开始最终复核
                </button>
              )}
              {showFinalReview && !projection.can_advance_final_review && (
                <div style={{ marginTop: 8, color: tokens.anyu.color.stone }}>等待管理员继续</div>
              )}
            </div>
          </div>
        )}

        {deleteNotice && (
          <div className="mcw-message-row mcw-message-assistant" role="status">
            <span className="mcw-avatar mcw-message-avatar" aria-hidden="true">月</span>
            <div className="mcw-bubble mcw-assistant-bubble">{deleteNotice}</div>
          </div>
        )}

        {!privacyMode && ["admin", "finance"].includes(projection.actor_role) && (
          <div style={{ marginBottom: 12 }}>
            <button type="button" className="mcw-secondary-button" style={{ ...touchButton, background: tokens.anyu.color.shell, color: tokens.anyu.color.ink.default, border: `1px solid ${tokens.anyu.color.linen}` }} disabled={sending || uploadingCount > 0} onClick={() => void followUpInvestigation("继续查保洁差异")}>
              查保洁差异 / 继续上次调查
            </button>
          </div>
        )}
        <WorkTimeline
          onFollowUp={(text, contextRunId) => void followUpInvestigation(text, contextRunId)}
          onOpenWorkflow={onOpenWorkflow}
          onOpenOrder={onOpenOrder}
          busy={privacyMode || sending || uploadingCount > 0}
          projection={projection}
          events={events}
          replies={replies}
          receipts={timelineReceipts}
          exchanges={exchanges}
          allowDocumentActions={!privacyMode && !showFinalReview}
          primaryDocumentId={actionLivesWithDocument ? actionTargetDocument?.document_id : undefined}
          primaryActionLabel={actionLivesWithDocument ? actionLabel : undefined}
          onPrimaryAction={!privacyMode && actionLivesWithDocument ? action.kind === "review_work_log" && actionTargetDocument
            ? () => reviewWorkLog(actionTargetDocument.document_id)
            : onPrimaryAction : undefined}
          onRetryAnalysis={onRetryAnalysis}
          onConfirmFields={onConfirmFields}
          onRequestDelete={canDeleteDocuments ? requestDelete : undefined}
          onSelectDocument={showFinalReview ? undefined : (documentId) => {
            const document = projection.sources.flatMap((source) => source.documents).find((item) => item.document_id === documentId);
            if (document) void selectDocument(document);
          }}
        />

        {taskAvailable && taskProposal?.scope === scopeKey && taskProposal.month === projection.billing_month
          && monthlyTask.task?.actions.some((action) => action.proposal_id === taskProposal.id) && <MonthlyCloseTaskProposal
          key={`${scopeKey}:${projection.billing_month}:${taskProposal.id}`}
          month={projection.billing_month}
          proposalId={taskProposal.id}
          onClose={() => setTaskProposal(null)}
          onFinished={monthlyTask.refresh}
        />}
        {inlineReview}

        {sending && (
          <div aria-label="正在处理的消息">
            <div className="mcw-message-row mcw-message-user">
              <div className="mcw-bubble mcw-user-bubble">{pendingMessage ?? "查看当前资料的核对结果"}</div>
            </div>
            <div className="mcw-message-row mcw-message-assistant" role="status" aria-label="月结助理处理状态" aria-live="polite" aria-atomic="true">
              <span className="mcw-avatar mcw-message-avatar" aria-hidden="true">月</span>
              <div className="mcw-bubble mcw-assistant-bubble">
                <div>正在处理<span className="mcw-processing-dots" aria-hidden="true">…</span></div>
                <div className="mcw-status-detail">{assistantProgress?.message ?? (waitingLong ? "等待时间较长，仍在等待结果。请勿重复发送。" : "正在等待助理返回结果，请勿重复发送。")}</div>
                {onStopMessage && assistantProgress?.stage === "understanding" && <button type="button" className="mcw-secondary-button" style={{ ...touchButton, marginTop: 8 }} onClick={onStopMessage}>停止本次回答</button>}
              </div>
            </div>
          </div>
        )}

        {actionableInboxItems.length > 0 && (
          <SmartUploadInbox
            items={actionableInboxItems}
            showUploader={false}
            disabled={privacyMode}
            compact
            onReceive={onReceiveInbox ?? (async () => { throw new Error("当前账号不能上传资料"); })}
            onClassify={onClassifyInbox}
            onSetSource={onSetInboxSource}
            onConfirm={onConfirmInbox}
            onPermanentDelete={privacyMode ? undefined : onPermanentDeleteInbox}
          />
        )}
      </main>

      {newReplies && <button type="button" className="mcw-secondary-button" style={{ ...touchButton, alignSelf: "center" }} onClick={() => {
        followLatest.current = true;
        manualScroll.current = false;
        setNewReplies(false);
        if (conversationRef.current) conversationRef.current.scrollTop = conversationRef.current.scrollHeight;
      }}>查看最新回复 ↓</button>}

      <form
        className={`mcw-composer${dragActive ? " is-dragging" : ""}`}
        aria-label="给月结助理发消息"
        onSubmit={(event) => { event.preventDefault(); void sendToAssistant(); }}
        onDragEnter={(event) => { event.preventDefault(); setDragActive(true); }}
        onDragOver={(event) => { event.preventDefault(); setDragActive(true); }}
        onDragLeave={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragActive(false);
        }}
        onDrop={handleDrop}
        onPaste={handlePaste}
      >
        <input
          ref={inputRef}
          aria-label="选择月结资料"
          type="file"
          accept={onReceiveCaseFile ? ".xls,.xlsx,.docx,.png,.jpg,.jpeg" : ".xls,.xlsx"}
          multiple
          disabled={privacyMode || !receiveComposerFile || sending || uploadingCount > 0}
          style={{ display: "none" }}
          onChange={(event) => {
            void receiveFiles(Array.from(event.target.files ?? []));
            event.target.value = "";
          }}
        />

        {uploadingCount > 0 && <div role="status" aria-label="资料上传进度" className="mcw-upload-progress">还剩 {uploadingCount} 份 · {uploadProgress ?? "正在接收"}</div>}
        {pendingFiles.length > 0 && (
          <div className="mcw-attachments" aria-label="待发送文件">
            {pendingFiles.map((file) => (
              <div key={file.attachmentId} role="status" className="mcw-attachment">
                {file.image ? <FileImageOutlined aria-hidden="true" /> : <FileExcelOutlined aria-hidden="true" />}
                <span>{file.filename}</span>
                <small>{file.detail ?? "已收到"}</small>
              </div>
            ))}
          </div>
        )}
        {onReceiveCaseFile && pendingFiles.length > 0 && <div className="mcw-upload-progress">原件已保存，尚未记账。可补一句“核对这些资料，先列出待确认项”，再点击发送。</div>}
        {composerError && <div role="alert" className="mcw-composer-error" style={{ whiteSpace: "pre-wrap" }}>{composerError}</div>}

        <textarea
          id="monthly-close-agent-message"
          aria-label="给月结助理的说明"
          disabled={privacyMode}
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          onCompositionStart={() => { composingMessage.current = true; }}
          onCompositionEnd={() => { composingMessage.current = false; }}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || event.shiftKey) return;
            // IME Enter selects a candidate; Safari can report only keyCode 229.
            if (composingMessage.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
            event.preventDefault();
            if (!event.repeat && !sending && uploadingCount === 0) event.currentTarget.form?.requestSubmit();
          }}
          aria-describedby="monthly-close-message-shortcuts"
          rows={2}
          placeholder={dragActive ? "松开即可添加文件" : onReceiveCaseFile ? "说明要核对什么，可同时添加多份 Excel 和截图" : "发消息，或把 Excel 文件拖到这里"}
        />
        <div id="monthly-close-message-shortcuts" className="mcw-composer-status">Enter 发送 · Shift + Enter 换行</div>
        <div className="mcw-composer-actions">
          <button
            type="button"
            className="mcw-icon-button"
            aria-label="添加账单文件"
            disabled={privacyMode || !receiveComposerFile || sending || uploadingCount > 0}
            onClick={() => inputRef.current?.click()}
          >
            <PaperClipOutlined aria-hidden="true" />
          </button>
          <span className="mcw-composer-status">{sending ? "正在处理，可以先写下一句" : uploadingCount > 0 ? "文件接收完成后可发送" : onReceiveCaseFile ? "可粘贴或拖入 Excel、Word 清单、截图" : "可粘贴或拖入 .xls / .xlsx 文件"}</span>
          <button
            type="submit"
            className="mcw-send-button"
            aria-label="发送给月结助理"
            disabled={privacyMode || sending || uploadingCount > 0 || (!message.trim() && pendingFiles.length === 0)}
          >
            {sending ? <span aria-hidden="true">…</span> : <ArrowUpOutlined aria-hidden="true" />}
          </button>
        </div>
      </form>

      <Drawer
        title={`${monthLabel(projection.billing_month)} · 本月进度`}
        width={420}
        open={progressOpen}
        onClose={() => setProgressOpen(false)}
      >
        <div style={{ color: tokens.anyu.color.stone }}>本月共收到 {allFiles.length} 份文件，包含聊天原件与分类资料；相同文件关联只计一次。</div>
        <ul aria-label="全部已收资料" style={{ paddingLeft: 20 }}>{allFiles.map((file) => <li key={file.id} style={{ padding: "8px 0", overflowWrap: "anywhere" }}>
          <strong>{file.filename}</strong><div>{file.state}</div>
          {file.sourceId && !privacyMode ? <button className="mcw-secondary-button" style={touchButton} disabled={sending} onClick={() => { setProgressOpen(false); void followUpInvestigation("核对这些资料", undefined, [file.sourceId!]); }}>查看识别与入账结果</button>
            : file.documentId ? <button className="mcw-secondary-button" style={touchButton} onClick={() => { const doc = projection.sources.flatMap(source => source.documents).find(doc => doc.document_id === file.documentId); if (doc) void selectDocument(doc); }}>查看文件依据</button> : <span>请在聊天中继续确认文件类别</span>}
        </li>)}</ul>
        <div style={{ marginTop: 18 }}>
          <SourceChecklist
            sources={projection.sources}
            receiptState={(type) => sourceReceiptState(projection, type)}
            onMarkNotApplicable={!privacyMode && onMarkNotApplicable ? (source) => {
              setNotApplicableError(null);
              setNotApplicableReason("");
              setNotApplicableTarget(source);
            } : undefined}
          />
        </div>

        {projection.actor_role === "admin" && !!projection.financial_case?.sources.length && <section className="mcw-evidence" aria-label="原件核对与待办">
          <h2 style={{ fontSize: 18, fontWeight: 500, margin: "0 0 14px" }}>原件核对与待办</h2>
          <p style={{ color: tokens.anyu.color.stone }}>以下状态与本月核对步骤同步。原件已保存、说明已确认和费用已入账分别显示。</p>
          {projection.financial_case.sources.map((source, index) => <div key={source.source_id} style={{ borderBottom: `1px solid ${tokens.anyu.color.linen}`, padding: "14px 0", overflowWrap: "anywhere" }}>
            <div>{evidenceFilename(source.filename, index)}</div>
            <p style={{ color: tokens.anyu.color.stone }}>识别内容 {source.fact_count} 条 · 已核实内容 {source.reviewed_fact_count} 条 · 通过此原件入账 {source.posted_fact_count} 条 · 已关联记录 {source.matched_fact_count} 条</p>
            <p>这里的入账数只统计与此原件建立关联的记录；显示 0 不代表系统没有该笔费用，应先查找已有记录。</p>
            {source.pending_count > 0 ? <details><summary>待处理 {source.pending_count} 项</summary>
              <ul>{projection.financial_case!.pending_issues.filter((issue) => issue.source_id === source.source_id).map((issue) => <li key={issue.issue_key}>{issue.message}</li>)}</ul>
            </details> : <p>当前原件没有未解决差异；月结是否完成请看各步骤进度。</p>}
            {!privacyMode && <button type="button" className="mcw-secondary-button" disabled={sending || uploadingCount > 0} style={{ ...touchButton, marginTop: 10, border: `1px solid ${tokens.anyu.color.linen}`, background: tokens.anyu.color.shell, color: tokens.anyu.color.ink.default }} onClick={() => { setProgressOpen(false); void followUpInvestigation("核对这些资料", undefined, [source.source_id]); }}>继续核对这份资料</button>}
          </div>)}
        </section>}

        <section className="mcw-evidence" aria-label="所选证据">
          <h2 style={{ fontSize: 18, fontWeight: 500, margin: "0 0 14px" }}>{showFinalReview ? "最终复核" : "文件与核对依据"}</h2>
          <EvidencePanel
            selected={renderedEvidence}
            loading={evidenceLoading}
            error={evidenceError}
            finalReview={showFinalReview ? projection.final_review : undefined}
            canAdvanceFinalReview={!privacyMode && projection.can_advance_final_review}
            onStartFinalReview={() => { setProgressOpen(false); onPrimaryAction(); }}
          />
          {!privacyMode && !showFinalReview && renderedEvidence && onReviewDocument && (
            <button type="button" className="mcw-primary-button" style={{ ...touchButton, width: "100%", marginTop: 14, border: `1px solid ${tokens.anyu.color.ink.default}`, background: tokens.anyu.color.ink.default, color: tokens.anyu.color.shell }} onClick={() => { setProgressOpen(false); if (renderedEvidence.analysis_state === "work_log_ready") reviewWorkLog(renderedEvidence.document_id); else onReviewDocument(renderedEvidence.document_id); }}>
              {renderedEvidence.analysis_state === "work_log_ready" ? "核对保洁记录与续住费用" : "继续处理此文件"}
            </button>
          )}
          {!showFinalReview && (
            <div style={{ marginTop: 18, display: "grid", gap: 8 }}>
              {projection.sources.flatMap((source) => source.documents).map((document) => (
                <div key={document.document_id} style={{ display: "grid", gridTemplateColumns: canDeleteDocuments ? "minmax(0, 1fr) auto" : "1fr", gap: 8 }}>
                  <button type="button" className="mcw-secondary-button" aria-label={`查看 ${document.filename ?? "所选资料"} 的证据`} style={{ ...touchButton, minWidth: 0, textAlign: "left", overflowWrap: "anywhere", border: `1px solid ${tokens.anyu.color.linen}`, background: tokens.anyu.color.shell, color: tokens.anyu.color.ink.default }} onClick={() => void selectDocument(document)}>
                    {document.filename ?? "查看文件"}
                  </button>
                  {canDeleteDocuments && (
                    <button type="button" className="mcw-secondary-button" aria-label={`从本月进度删除文件 ${document.filename ?? "所选资料"}`} style={{ ...touchButton, border: `1px solid ${tokens.anyu.color.linen}`, background: tokens.anyu.color.shell, color: tokens.anyu.color.clay }} onClick={() => requestDelete(document)}>
                      删除
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>
      </Drawer>

      <Modal
        title={`确认本月不需要「${notApplicableTarget ? sourceLabel(notApplicableTarget.source_type) : "此项资料"}」？`}
        open={notApplicableTarget != null}
        okText="确认本月不需要"
        cancelText="取消"
        okButtonProps={{ disabled: !notApplicableReason.trim() }}
        cancelButtonProps={{ disabled: markingNotApplicable }}
        confirmLoading={markingNotApplicable}
        closable={!markingNotApplicable}
        maskClosable={!markingNotApplicable}
        onCancel={() => {
          if (markingNotApplicable) return;
          setNotApplicableTarget(null);
          setNotApplicableReason("");
          setNotApplicableError(null);
        }}
        onOk={() => { void confirmNotApplicable(); }}
      >
        <div style={{ display: "grid", gap: 12, lineHeight: 1.7 }}>
          <div>确认后，月结助理不会再要求你上传这一项；以后如果有文件，仍可重新上传。</div>
          <textarea
            aria-label="本月没有此项的原因"
            value={notApplicableReason}
            onChange={(event) => setNotApplicableReason(event.target.value)}
            placeholder="例如：本月没有安排保洁"
            rows={3}
            style={{ width: "100%", border: `1px solid ${tokens.anyu.color.linen}`, borderRadius: 8, padding: 10, background: tokens.anyu.color.shell, color: tokens.anyu.color.ink.default, font: "inherit", resize: "vertical", boxSizing: "border-box" }}
          />
          {notApplicableError && <div role="alert" style={{ color: tokens.anyu.color.clay }}>{notApplicableError}</div>}
        </div>
      </Modal>

      <Modal
        title="永久删除这份文件？"
        open={deleteTarget != null}
        okText="永久删除"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        cancelButtonProps={{ disabled: deleting }}
        confirmLoading={deleting}
        closable={!deleting}
        maskClosable={!deleting}
        onCancel={() => {
          if (deleting) return;
          setDeleteTarget(null);
          setDeleteError(null);
        }}
        onOk={() => { void confirmDelete(); }}
      >
        <div style={{ display: "grid", gap: 12, lineHeight: 1.7 }}>
          <div style={{ fontWeight: 500, overflowWrap: "anywhere" }}>{deleteTarget?.filename ?? "这份月结资料"}</div>
          <div>删除后无法恢复，以后需要核对时必须重新上传。</div>
          <div style={{ color: tokens.anyu.color.stone }}>
            系统会先把文件从本月对账中移除，再永久删除原文件。若它已进入正式对账、结算或审批，系统会停止永久删除并保留账目凭证。
          </div>
          {deleteError && <div role="alert" style={{ color: tokens.anyu.color.clay }}>{deleteError}</div>}
        </div>
      </Modal>

      <style>{`
        .mcw-chat { width:min(920px,100%); margin:0 auto; height: min(900px, calc(100dvh - 160px)); min-height:560px; border:0.5px solid ${tokens.anyu.color.linen}; border-radius:12px; background:${tokens.anyu.color.shell}; display:flex; flex-direction:column; overflow:hidden; }
        .mcw-header-actions { display:flex; flex-wrap:wrap; gap:8px; min-width:0; }
        .mcw-chat-header { flex-shrink:0; min-height:68px; padding:11px 18px; border-bottom:0.5px solid ${tokens.anyu.color.linen}; display:flex; align-items:center; justify-content:space-between; gap:16px; background:${tokens.anyu.color.shell}; }
        .mcw-assistant-identity { display:flex; align-items:center; gap:10px; min-width:0; }
        .mcw-assistant-identity strong,.mcw-assistant-identity small { display:block; }
        .mcw-assistant-identity strong { font-weight:500; }
        .mcw-assistant-identity small { color:${tokens.anyu.color.stone}; margin-top:2px; }
        .mcw-avatar { width:36px; height:36px; flex:0 0 36px; border-radius:999px; display:grid; place-items:center; background:${tokens.anyu.color.ink.default}; color:${tokens.anyu.color.shell}; font-size:14px; }
        .mcw-progress-button { min-height:44px; border:1px solid ${tokens.anyu.color.linen}; border-radius:999px; padding:0 18px; background:${tokens.anyu.color.shell}; color:${tokens.anyu.color.ink.default}; font:inherit; transition:border-color 160ms var(--ease-breath),background 160ms var(--ease-breath),color 160ms var(--ease-breath); }
        .mcw-progress-button:hover,.mcw-icon-button:hover:not(:disabled),.mcw-secondary-button:hover:not(:disabled) { border-color:${tokens.anyu.color.driftwood}; background:${tokens.anyu.color.sand}; }
        .mcw-primary-button:hover:not(:disabled),.mcw-send-button:hover:not(:disabled) { border-color:${tokens.anyu.color.ink.hover}; background:${tokens.anyu.color.ink.hover}; }
        .mcw-conversation { flex:1; min-height:0; overflow-y:auto; overscroll-behavior:contain; padding:26px 22px 14px; display:flex; flex-direction:column; gap:20px; }
        .mcw-timeline { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:22px; }
        .mcw-document-exchange,.mcw-freeform-exchange { list-style:none; display:flex; flex-direction:column; gap:8px; }
        .mcw-message-row { width:100%; display:flex; gap:10px; align-items:flex-start; }
        .mcw-message-user { justify-content:flex-end; }
        .mcw-message-avatar { width:30px; height:30px; flex-basis:30px; font-size:12px; }
        .mcw-bubble { max-width:min(680px,calc(100% - 42px)); border-radius:12px; padding:13px 15px; overflow-wrap:anywhere; }
        .mcw-assistant-bubble { background:${tokens.anyu.color.sand}; border:0.5px solid ${tokens.anyu.color.linen}; }
        .mcw-user-bubble { background:${tokens.anyu.color.ink.default}; color:${tokens.anyu.color.shell}; }
        .mcw-file-bubble { max-width:min(520px,calc(100% - 42px)); padding:11px 14px; }
        .mcw-file-type { color:${tokens.anyu.color.driftwood}; font-size:11px; }
        .mcw-file-name { margin-top:4px; overflow-wrap:anywhere; }
        .mcw-status-bubble { max-width:min(620px,calc(100% - 42px)); }
        .mcw-stage { font-weight:500; }
        .mcw-stage-problem { color:${tokens.anyu.color.clay}; }
        .mcw-stage-completed { color:${tokens.anyu.color.sage}; }
        .mcw-status-detail { margin-top:5px; color:${tokens.anyu.color.stone}; line-height:1.65; }
        .mcw-inline-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:11px; }
        .mcw-inline-actions:empty { display:none; }
        .mcw-danger-button { color:${tokens.anyu.color.clay} !important; }
        .mcw-system-event { color:${tokens.anyu.color.stone}; font-size:12px; text-align:center; }
        .mcw-degraded-note { margin-top:8px; color:${tokens.anyu.color.clay}; }
        .mcw-composer { flex-shrink:0; margin:14px 18px 18px; border:1px solid ${tokens.anyu.color.linen}; border-radius:16px; padding:11px 12px 10px; background:${tokens.anyu.color.shell}; transition:border-color 160ms var(--ease-breath),background 160ms var(--ease-breath); }
        .mcw-composer.is-dragging { border-color:${tokens.anyu.color.ink.default}; background:${tokens.anyu.color.sand}; }
        .mcw-composer:focus-within { border-color:${tokens.anyu.color.stone}; }
        .mcw-composer textarea { display:block; width:100%; min-height:54px; max-height:180px; border:0; outline:0; background:transparent; color:${tokens.anyu.color.ink.default}; padding:4px 4px 8px; font:inherit; font-size:16px; line-height:1.6; resize:none; box-sizing:border-box; }
        .mcw-composer textarea::placeholder { color:${tokens.anyu.color.driftwood}; }
        .mcw-composer-actions { display:flex; align-items:center; gap:9px; }
        .mcw-icon-button,.mcw-send-button { width:44px; height:44px; flex:0 0 44px; border-radius:999px; display:grid; place-items:center; font-size:18px; transition:border-color 160ms var(--ease-breath),background 160ms var(--ease-breath),color 160ms var(--ease-breath); }
        .mcw-primary-button,.mcw-secondary-button { transition:border-color 160ms var(--ease-breath),background 160ms var(--ease-breath),color 160ms var(--ease-breath); }
        .mcw-icon-button { border:1px solid ${tokens.anyu.color.linen}; background:${tokens.anyu.color.shell}; color:${tokens.anyu.color.ink.default}; }
        .mcw-processing-dots { animation:mcw-processing-pulse 1.4s ease-in-out infinite; }
        @keyframes mcw-processing-pulse { 50% { opacity:.3; } }
        @media (prefers-reduced-motion:reduce) { .mcw-processing-dots { animation:none; } }
        .mcw-send-button { margin-left:auto; border:1px solid ${tokens.anyu.color.ink.default}; background:${tokens.anyu.color.ink.default}; color:${tokens.anyu.color.shell}; }
        .mcw-icon-button:disabled,.mcw-send-button:disabled { opacity:.38; cursor:not-allowed; }
        .mcw-composer-status { color:${tokens.anyu.color.stone}; font-size:12px; }
        .mcw-upload-progress { color:${tokens.anyu.color.stone}; font-size:12px; padding:2px 4px 8px; overflow-wrap:anywhere; }
        .mcw-attachments { display:flex; flex-wrap:wrap; gap:8px; padding:2px 2px 9px; }
        .mcw-attachment { max-width:100%; min-height:42px; display:flex; align-items:center; gap:8px; border:0.5px solid ${tokens.anyu.color.linen}; border-radius:8px; padding:7px 10px; background:${tokens.anyu.color.sand}; }
        .mcw-attachment span { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .mcw-attachment small { color:${tokens.anyu.color.sage}; white-space:nowrap; }
        .mcw-composer-error { color:${tokens.anyu.color.clay}; padding:2px 4px 8px; }
        .mcw-evidence { border-top:0.5px solid ${tokens.anyu.color.linen}; margin-top:22px; padding-top:20px; }
        .mcw-chat button:focus-visible { outline:2px solid ${tokens.anyu.color.ink.default}; outline-offset:2px; }
        .mcw-chat .mcw-composer textarea:focus-visible { outline:none; }
        @media (max-width:1023px) {
          .mcw-chat { min-height:600px; }
          .mcw-chat-header { padding:10px 14px; }
          .mcw-conversation { padding:20px 14px 10px; }
          .mcw-composer { margin:10px 12px 14px; }
          .mcw-progress-button { padding:0 13px; }
          .mcw-composer-status { display:none; }
        }
        @media (max-width:640px) {
          .mcw-chat { height:auto; min-height:calc(100dvh - 180px); border-left:0; border-right:0; border-radius:0; }
          .mcw-chat-header { align-items:flex-start; flex-wrap:wrap; }
          .mcw-assistant-identity small { max-width:190px; }
          .mcw-progress-button { min-height:44px; padding:0 11px; font-size:13px; white-space:nowrap; }
          .mcw-conversation { flex:none; height:55dvh; min-height:280px; padding:18px 10px 8px; gap:16px; }
          .mcw-timeline { gap:18px; }
          .mcw-bubble,.mcw-file-bubble,.mcw-status-bubble { max-width:calc(100% - 38px); }
          .mcw-inline-actions { display:grid; grid-template-columns:1fr; }
          .mcw-inline-actions button { width:100%; }
          /* Keep focus/scrollIntoView above the fixed bottom nav and order FAB. */
          .mcw-composer { margin:8px 8px 10px; scroll-margin-bottom:calc(120px + env(safe-area-inset-bottom, 0px)); }
        }
        @media (prefers-reduced-motion:reduce) { * { scroll-behavior:auto !important; transition:none !important; } }
      `}</style>
    </section>
  );
}
