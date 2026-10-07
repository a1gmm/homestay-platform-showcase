import { ExecutionReceipt } from "./ExecutionReceipt";
import type { AssistantReply, MonthlyCloseEvent, MonthlyCloseProjection, MonthlyCloseReceiptView } from "@/lib/monthly-close";
import { presentDocumentProgress } from "@/lib/monthly-close-presentation";
import { tokens } from "@/lib/design-tokens";
import { sourceLabel } from "./SourceChecklist";
import { CleaningWorkChatReply } from "./CleaningWorkChatReply";
import { CleaningInvestigationReply } from "./CleaningInvestigationReply";
import { FormattedResult } from "./FormattedResult";
import { QueryResultActions } from "./QueryResultActions";
import { ProgressDetails, hasStructuredProgress } from "./ProgressDetails";
import { ConversationMemoryReply } from "./ConversationMemoryReply";
import { ChatWorkflowActions } from "./ChatWorkflowActions";
import { FinancialCaseReply } from "./FinancialCaseReply";
import { OrderIdentityChatReply } from "./OrderIdentityChatReply";

const touchButton = { minHeight: 44, borderRadius: 999, padding: "0 16px", border: `1px solid ${tokens.anyu.color.linen}`, background: tokens.anyu.color.shell, color: tokens.anyu.color.ink.default } as const;
const classificationLabel = { pending: "已上传，等待识别", needs_confirmation: "待你确认资料类型", confirmed: "资料类型已确认", failed: "资料类型未识别" } as const;
const processingJobLabel = { queued: "正在识别", processing: "正在识别", needs_review: "待你确认识别结果", completed: "识别完成", failed_safe: "识别未完成" } as const;
const eventLabel: Record<string, string> = {
  "reconciliation.completed": "核对结果已更新",
};

function receiptDetail(receipt: MonthlyCloseReceiptView) {
  if (receipt.processing_job_state === "failed_safe" || receipt.classification_state === "failed") {
    return "文件已经保存。请重新识别；仍然失败时，让管理员调整读取方式。";
  }
  if (receipt.processing_job_state === "queued") {
    return "文件已经保存，正在等待系统读取。你可以留在本页查看进度。";
  }
  return "文件已经保存，系统会在读取完成后告诉你下一步。";
}

export function WorkTimeline({
  projection,
  events,
  replies,
  receipts = [],
  exchanges = [],
  allowDocumentActions = true,
  primaryDocumentId,
  primaryActionLabel,
  onPrimaryAction,
  onRetryAnalysis,
  onConfirmFields,
  onSelectDocument,
  onRequestDelete,
  onFollowUp,
  onOpenOrder,
  onOpenWorkflow,
  busy,
}: {
  projection: MonthlyCloseProjection;
  events: MonthlyCloseEvent[];
  replies: AssistantReply[];
  receipts?: MonthlyCloseReceiptView[];
  exchanges?: Array<{ id: string; userText: string; assistantReply?: AssistantReply }>;
  allowDocumentActions?: boolean;
  primaryDocumentId?: string;
  primaryActionLabel?: string;
  onPrimaryAction?: () => void;
  onRetryAnalysis: (documentId: string) => void;
  onConfirmFields: (documentId: string) => void;
  onSelectDocument?: (documentId: string) => void;
  onRequestDelete?: (document: MonthlyCloseProjection["sources"][number]["documents"][number]) => void;
  onFollowUp?: (text: string, contextRunId: string) => void;
  onOpenOrder?: (orderId: string) => void;
  onOpenWorkflow?: (focus: string) => void;
  busy?: boolean;
}) {
  const turns = [...replies];
  for (const exchange of exchanges) if (exchange.assistantReply && !turns.some((reply) => reply.run_id === exchange.assistantReply?.run_id)) turns.push(exchange.assistantReply);
  const latestFinancialCase = turns.findLastIndex((reply) => reply.tool === "financial_case");
  const latestWorkChange = turns.findLastIndex((reply) => reply.tool === "cleaning_work_chat" && (reply.facts.state !== "report" || Boolean(reply.facts.reason)));
  const latestOrderChange = turns.findLastIndex((reply) => reply.tool === "order_identity_chat" && reply.facts.state !== "clarification");
  const documents = projection.sources.flatMap((source) => source.documents.map((document) => ({ document, source })));
  return (
    <ol aria-label="月结工作记录" aria-live="polite" className="mcw-timeline">
      {documents.map(({ document, source }) => {
        const filename = document.filename ?? sourceLabel(document.source_type);
        const progress = presentDocumentProgress(document, source.state);
        const isPrimaryTarget = primaryDocumentId === document.document_id && primaryActionLabel && onPrimaryAction;
        return (
          <li key={document.document_id} role="group" aria-label={`${filename} 的处理进度`} className="mcw-document-exchange">
            <div className="mcw-message-row mcw-message-user">
              <div className="mcw-bubble mcw-user-bubble mcw-file-bubble">
                <div className="mcw-file-type">{sourceLabel(document.source_type)}</div>
                <div className="mcw-file-name">{filename}</div>
              </div>
            </div>
            <div className="mcw-message-row mcw-message-assistant">
              <span className="mcw-avatar mcw-message-avatar" aria-hidden="true">月</span>
              <div className="mcw-bubble mcw-assistant-bubble mcw-status-bubble">
                <div className={`mcw-stage mcw-stage-${progress.stage}`}>{progress.label}</div>
                <div className="mcw-status-detail">{progress.detail}</div>
                <div className="mcw-inline-actions">
                  {isPrimaryTarget && (
                    <button type="button" className="mcw-primary-button" style={{ ...touchButton, color: tokens.anyu.color.shell, background: tokens.anyu.color.ink.default, borderColor: tokens.anyu.color.ink.default }} onClick={onPrimaryAction}>
                      {primaryActionLabel}
                    </button>
                  )}
                  {onSelectDocument && (
                    <button type="button" className="mcw-secondary-button" aria-label={`查看 ${filename} 的证据`} style={touchButton} onClick={() => onSelectDocument(document.document_id)}>查看详情</button>
                  )}
                  {allowDocumentActions && progress.stage === "problem" && document.storage_state === "stored" && (
                    <>
                      <button type="button" className="mcw-secondary-button" style={touchButton} onClick={() => onRetryAnalysis(document.document_id)}>重新识别</button>
                      <button type="button" className="mcw-secondary-button" style={touchButton} onClick={() => onConfirmFields(document.document_id)}>调整读取方式</button>
                    </>
                  )}
                  {allowDocumentActions && onRequestDelete && (
                    <button type="button" className="mcw-secondary-button mcw-danger-button" aria-label={`删除文件 ${filename}`} style={touchButton} onClick={() => onRequestDelete(document)}>删除文件</button>
                  )}
                </div>
              </div>
            </div>
          </li>
        );
      })}
      {receipts.map((receipt) => (
        <li key={receipt.receipt_id} role="group" aria-label={`${receipt.filename} 的处理进度`} className="mcw-document-exchange">
          <div className="mcw-message-row mcw-message-user">
            <div className="mcw-bubble mcw-user-bubble mcw-file-bubble">
              <div className="mcw-file-type">Excel 文件</div>
              <div className="mcw-file-name">{receipt.filename}</div>
            </div>
          </div>
          <div className="mcw-message-row mcw-message-assistant">
            <span className="mcw-avatar mcw-message-avatar" aria-hidden="true">月</span>
            <div className="mcw-bubble mcw-assistant-bubble mcw-status-bubble">
              <div className="mcw-stage">
                {receipt.processing_job_state ? processingJobLabel[receipt.processing_job_state] : classificationLabel[receipt.classification_state]}
              </div>
              <div className="mcw-status-detail">{receiptDetail(receipt)}</div>
            </div>
          </div>
        </li>
      ))}
      {events.flatMap((event) => {
        const label = eventLabel[event.kind];
        return label ? [(
          <li key={event.event_id} aria-label="系统事件" className="mcw-system-event">{label}</li>
        )] : [];
      })}
      {exchanges.filter((exchange) => !exchange.assistantReply).map((exchange) => <li key={exchange.id} className="mcw-message-row mcw-message-user"><div className="mcw-bubble mcw-user-bubble">{exchange.userText}</div></li>)}
      {turns.map((reply, turnIndex) => {
        if (reply.tool === "financial_case" && (projection.actor_role !== "admin" || reply.facts.billing_month !== projection.billing_month)) return null;
        const userText = exchanges.find((exchange) => exchange.assistantReply?.run_id === reply.run_id)?.userText
          ?? (typeof reply.facts.request_text === "string" ? reply.facts.request_text : undefined);
        return <li key={reply.run_id} aria-label="助理判断" className="mcw-freeform-exchange">
          {userText && <div className="mcw-message-row mcw-message-user"><div className="mcw-bubble mcw-user-bubble">{userText}</div></div>}
          <details open={turnIndex === turns.length - 1 || reply.facts.state === "proposal" && ((reply.tool === "financial_case" && turnIndex === latestFinancialCase) || (reply.tool === "cleaning_work_chat" && turnIndex === latestWorkChange) || (reply.tool === "order_identity_chat" && turnIndex === latestOrderChange))}>
            <summary style={{ minHeight: 44, cursor: "pointer", color: "var(--stone)", display: "flex", alignItems: "center" }}>{turnIndex === turns.length - 1 ? "最近一次回复" : `查看此前回复 ${turnIndex + 1} · 当前进度以上方结果为准`}</summary>
          <div aria-label="助理判断" className="mcw-message-row mcw-message-assistant">
            <span className="mcw-avatar mcw-message-avatar" aria-hidden="true">月</span>
            <div className="mcw-bubble mcw-assistant-bubble">
              <ExecutionReceipt reply={reply} projection={projection} />
              {!reply.output && reply.tool !== "financial_case" && !hasStructuredProgress(reply, projection) && <div style={{ whiteSpace: "pre-wrap" }}>{reply.message}</div>}
              {!reply.output && !hasStructuredProgress(reply, projection) && reply.tool === "review_month" && !["cost_responsibility", "amount_summary"].includes(String(reply.facts.query_mode)) && projection.actor_role === "admin" && reply.facts.billing_month === projection.billing_month && <details style={{ marginTop: 16 }}>
                <summary style={{ minHeight: 44, cursor: "pointer" }}>查看各类资料和月结进度</summary>
                <ul style={{ paddingLeft: 20, display: "grid", gap: 16 }}>
                  {(reply.facts.sources as Array<{ source_type: string; label: string; detail: string; next_step: string }>).map((source) => <li key={source.source_type}>
                    <div>{source.label}：{source.detail}</div>
                    <button type="button" style={{ ...touchButton, marginTop: 8 }} disabled={busy || !onFollowUp} onClick={() => onFollowUp?.(`查看${source.source_type === "cleaning_statement" ? "保洁费用" : source.label}的当前进度`, reply.run_id)}>继续看{source.label}</button>
                  </li>)}
                </ul>
                <ul style={{ paddingLeft: 20, lineHeight: 2 }}>
                  {(reply.facts.steps as Array<{ step_key: string; label: string; status: string; blocking_count: number }>).map((step) => <li key={step.step_key}>{step.label}：{step.status === "confirmed" ? "已确认" : step.blocking_count ? `${step.blocking_count} 项待处理` : "尚待确认"}</li>)}
                </ul>
              </details>}
              <FormattedResult reply={reply} projection={projection} />
              <ProgressDetails key={`${reply.run_id}-${projection.billing_month}-${projection.actor_role}`} reply={reply} projection={projection} onSelectDocument={onSelectDocument} onOpenOrder={onOpenOrder} onOpenWorkflow={onOpenWorkflow} />
              {!reply.output && <FinancialCaseReply key={`financial-case-${reply.run_id}-${projection.billing_month}-${projection.actor_role}`} reply={reply} projection={projection} onFollowUp={onFollowUp} busy={busy} historical={turnIndex < latestFinancialCase} />}
              {!reply.output && <CleaningWorkChatReply reply={reply} projection={projection} onFollowUp={onFollowUp} busy={busy} historical={turnIndex < latestWorkChange} />}
              {!reply.output && <OrderIdentityChatReply reply={reply} projection={projection} onFollowUp={onFollowUp} busy={busy} historical={turnIndex < latestOrderChange} />}
              {!reply.output && <CleaningInvestigationReply reply={reply} projection={projection} onFollowUp={onFollowUp} onSelectDocument={onSelectDocument} busy={busy} />}
              {!hasStructuredProgress(reply, projection) && <QueryResultActions reply={reply} projection={projection} />}
              {!hasStructuredProgress(reply, projection) && <ChatWorkflowActions reply={reply} projection={projection} onOpenWorkflow={onOpenWorkflow} onOpenOrder={onOpenOrder} />}
              <ConversationMemoryReply reply={reply} projection={projection} />
              {reply.tool === "get_document_status" && projection.actor_role === "admin" && Array.isArray(reply.facts.documents) && (reply.facts.documents as Array<{document_id?: string}>).map((item) => {
                const doc = projection.sources.flatMap((source) => source.documents).find((doc) => doc.document_id === item.document_id);
                return doc && onSelectDocument ? <button key={doc.document_id} type="button" style={{ ...touchButton, marginTop: 12 }} onClick={() => onSelectDocument(doc.document_id)}>查看《{doc.filename}》的证据</button> : null;
              })}
              {reply.tool === "agent_clarification" && reply.facts.retryable === true && typeof reply.facts.request_text === "string" && <button type="button" style={{ ...touchButton, marginTop: 12 }} disabled={busy || !onFollowUp} onClick={() => onFollowUp?.(reply.facts.request_text as string, reply.run_id)}>重试这句话</button>}
              {reply.tool === "agent_clarification" && reply.facts.retryable === true && projection.actor_role === "admin" && <button type="button" style={{ ...touchButton, marginTop: 12 }} disabled={busy || !onFollowUp} onClick={() => onFollowUp?.("查看本月还有哪些待处理事项", reply.run_id)}>先查看本月待办</button>}
              {reply.tool !== "financial_case" && reply.narration_degraded && !["cost_responsibility", "amount_summary"].includes(String(reply.facts.query_mode)) && !hasStructuredProgress(reply, projection) && <div role="status" className="mcw-degraded-note">{reply.tool === "review_month" ? "已直接查询系统记录。" : "说明暂时没有生成，不影响已经保存的文件和当前操作。"}</div>}
            </div>
          </div>
          </details>
        </li>;
      })}
    </ol>
  );
}
