import { useRef, useState } from "react";
import { Alert, Button, Modal, Select, Space } from "antd";
import { InboxOutlined } from "@ant-design/icons";

import { extractErrorMessage } from "@/lib/api-errors";
import type { MonthlyCloseDurableReceipt, MonthlyCloseInboxItem } from "@/lib/monthly-close";
import { MONTHLY_CLOSE_ACTIVE_SOURCE_OPTIONS } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";

const touchControl = { minHeight: 44 } as const;

function statusText(item: MonthlyCloseInboxItem) {
  if (item.status === "dismissed") return "已从本月移除；可以重新使用，也可以永久删除";
  if (item.analysis_status === "queued") return "分类已确认，正在排队分析";
  if (item.analysis_status === "processing") return "分类已确认，后台正在分析";
  if (item.analysis_status === "needs_review") {
    return item.analysis_error || "原文件已保存，分析结果需要手动确认字段映射";
  }
  if (item.analysis_status === "failed_safe") return "原文件已保存，分析稍后会安全重试";
  if (item.processing_status === "queued") return "原文件已保存，正在排队识别";
  if (item.processing_status === "processing") return "原文件已保存，后台正在识别";
  if (item.processing_status === "needs_review") {
    return "原文件已保存，识别需要手动确认资料类型，无需重新上传";
  }
  if (item.processing_status === "failed_safe") return "原文件已保存，识别稍后会安全重试";
  if (item.status === "received") return "已收件，等待识别";
  if (item.status === "needs_review") return item.classification_reason || "识别把握不足，请确认资料类型";
  if (item.status === "classified") return item.classification_reason || "已识别，等待确认归档";
  if (item.status === "confirmed") return "已归档到本月资料";
  return item.last_error || "处理失败，请重试";
}

function compactStatusText(item: MonthlyCloseInboxItem) {
  if (item.status === "dismissed") return "已从本月移除";
  if (item.status === "failed") return "暂时没有读出来";
  if (item.status === "classified" || item.status === "needs_review") return "等待你确认";
  if (item.status === "confirmed") return "已开始核对";
  return "已保存，正在识别";
}

export function SmartUploadInbox({ items, disabled, showUploader = true, compact = false, onReceive, onClassify, onSetSource, onConfirm, onPermanentDelete }: {
  items: MonthlyCloseInboxItem[];
  disabled?: boolean;
  showUploader?: boolean;
  compact?: boolean;
  onReceive: (file: File) => Promise<MonthlyCloseDurableReceipt>;
  onClassify: (itemId: string) => Promise<unknown>;
  onSetSource: (itemId: string, sourceType: string) => Promise<unknown>;
  onConfirm: (itemId: string) => Promise<unknown>;
  onPermanentDelete?: (itemId: string) => Promise<unknown>;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [transferCount, setTransferCount] = useState(0);
  const [localError, setLocalError] = useState<string | null>(null);
  const [storedMessages, setStoredMessages] = useState<string[]>([]);
  const [deleteCandidate, setDeleteCandidate] = useState<MonthlyCloseInboxItem | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [editingIds, setEditingIds] = useState<Set<string>>(new Set());

  const withBusy = async (itemId: string, task: () => Promise<unknown>) => {
    setBusyIds((current) => new Set(current).add(itemId));
    try { await task(); } finally {
      setBusyIds((current) => {
        const next = new Set(current);
        next.delete(itemId);
        return next;
      });
    }
  };

  const runItem = async (item: MonthlyCloseInboxItem, task: () => Promise<unknown>) => {
    setLocalError(null);
    try {
      await withBusy(item.item_id, task);
    } catch (error) {
      setLocalError(extractErrorMessage(error, `“${item.filename}”处理失败，请重试`));
    }
  };

  const receiveFiles = async (files: File[]) => {
    setLocalError(null);
    setStoredMessages([]);
    setTransferCount(files.length);
    for (const file of files) {
      try {
        await onReceive(file);
        setStoredMessages((current) => [
          ...current,
          `${file.name} 已保存，后台正在排队识别，无需重复上传`,
        ]);
      } catch (error) {
        setLocalError(extractErrorMessage(error, `“${file.name}”收件失败，请重试`));
      } finally {
        setTransferCount((current) => Math.max(0, current - 1));
      }
    }
  };

  const readyItems = items.filter((item) => (
    item.status === "classified"
      || (item.status === "needs_review" && Boolean(item.source_type))
  ));
  const confirmReady = async () => {
    setLocalError(null);
    const failedNames: string[] = [];
    for (const item of readyItems) {
      try {
        await withBusy(item.item_id, () => onConfirm(item.item_id));
      } catch {
        failedNames.push(item.filename);
      }
    }
    if (failedNames.length) {
      setLocalError(`${failedNames.join("、")}归档失败，其他文件已继续处理；请修正后重试。`);
    }
  };

  const confirmPermanentDelete = async () => {
    if (!deleteCandidate || !onPermanentDelete) return;
    setDeleteError(null);
    setLocalError(null);
    try {
      await withBusy(
        deleteCandidate.item_id,
        () => onPermanentDelete(deleteCandidate.item_id),
      );
      setDeleteCandidate(null);
    } catch (error) {
      setDeleteError(
        extractErrorMessage(error, `“${deleteCandidate.filename}”没有删除，请重试`),
      );
    }
  };

  if (compact) {
    return (
      <>
        <section id="monthly-close-inbox" aria-label="月结资料收件记录" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          {localError && <Alert type="error" showIcon message={localError} />}
          {items.map((item) => {
            const busy = busyIds.has(item.item_id);
            const editing = editingIds.has(item.item_id) || !item.source_type;
            const sourceName = item.source_type
              ? MONTHLY_CLOSE_ACTIVE_SOURCE_OPTIONS.find((option) => option.value === item.source_type)?.label ?? "其他资料"
              : null;
            const waiting = item.status === "received";
            const needsConfirmation = item.status === "classified" || item.status === "needs_review";
            return (
              <div key={item.item_id} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <div className="mcw-message-row mcw-message-user">
                  <div className="mcw-bubble mcw-user-bubble">
                    <div style={{ color: tokens.anyu.color.driftwood, fontSize: 11 }}>Excel 文件</div>
                    <div style={{ marginTop: 4, overflowWrap: "anywhere" }}>{item.filename}</div>
                    <div style={{ marginTop: 5, color: tokens.anyu.color.linen, fontSize: 12 }}>{compactStatusText(item)}</div>
                  </div>
                </div>

                <div className="mcw-message-row mcw-message-assistant">
                  <span className="mcw-avatar mcw-message-avatar" aria-hidden="true">月</span>
                  <div className="mcw-bubble mcw-assistant-bubble">
                    {waiting && (
                      <>
                        <div>文件已经保存。我正在判断它是什么资料，你不用重新上传。</div>
                        <Button style={{ ...touchControl, marginTop: 12 }} loading={busy} disabled={disabled} aria-label={`继续识别${item.filename}`} onClick={() => void runItem(item, () => onClassify(item.item_id))}>现在识别</Button>
                      </>
                    )}

                    {needsConfirmation && !editing && sourceName && (
                      <>
                        <div>我认为这是<strong>“{sourceName}”</strong>。判断正确的话，我就按这个方向开始核对。</div>
                        {item.confidence !== null && <div style={{ color: tokens.anyu.color.stone, fontSize: 12, marginTop: 6 }}>识别把握：{Math.round(item.confidence * 100)}%</div>}
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
                          <Button type="primary" style={touchControl} loading={busy} disabled={disabled} aria-label={`确认${item.filename}为${sourceName}`} onClick={() => void runItem(item, () => onConfirm(item.item_id))}>对，开始核对</Button>
                          <Button style={touchControl} disabled={disabled || busy} onClick={() => setEditingIds((current) => new Set(current).add(item.item_id))}>不是这个</Button>
                        </div>
                      </>
                    )}

                    {needsConfirmation && editing && (
                      <>
                        <div>这是什么资料？请选择最接近的一项：</div>
                        <div role="group" aria-label={`${item.filename}资料类型`} style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
                          {MONTHLY_CLOSE_ACTIVE_SOURCE_OPTIONS.map((option) => (
                            <Button
                              key={option.value}
                              style={touchControl}
                              loading={busy && option.value === item.source_type}
                              disabled={disabled || busy}
                              onClick={() => void runItem(item, async () => {
                                await onSetSource(item.item_id, option.value);
                                setEditingIds((current) => {
                                  const next = new Set(current);
                                  next.delete(item.item_id);
                                  return next;
                                });
                              })}
                            >
                              {option.label}
                            </Button>
                          ))}
                        </div>
                      </>
                    )}

                    {item.status === "failed" && (
                      <>
                        <div>这次没有读出来，但文件还在。你可以直接重试。</div>
                        <Button style={{ ...touchControl, marginTop: 12 }} loading={busy} disabled={disabled} onClick={() => void runItem(item, () => item.source_type ? onConfirm(item.item_id) : onClassify(item.item_id))}>{item.source_type ? "重新开始核对" : "重新识别"}</Button>
                      </>
                    )}

                    {item.status === "dismissed" && (
                      <>
                        <div>这份文件已从本月移除。{onPermanentDelete ? "你可以重新使用，也可以永久删除。" : "需要时可以重新使用。"}</div>
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
                          {item.source_type && <Button style={touchControl} loading={busy} disabled={disabled} aria-label={`重新使用${item.filename}`} onClick={() => void runItem(item, () => onSetSource(item.item_id, item.source_type as string))}>重新使用</Button>}
                          {onPermanentDelete && <Button danger style={touchControl} loading={busy} disabled={disabled} aria-label={`永久删除${item.filename}`} onClick={() => { setDeleteError(null); setDeleteCandidate(item); }}>永久删除</Button>}
                        </div>
                      </>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </section>
        <Modal
          title="永久删除这份文件？"
          open={deleteCandidate !== null}
          okText="永久删除"
          cancelText="取消"
          confirmLoading={Boolean(deleteCandidate && busyIds.has(deleteCandidate.item_id))}
          okButtonProps={{ danger: true }}
          maskClosable={false}
          onCancel={() => { setDeleteCandidate(null); setDeleteError(null); }}
          onOk={() => { void confirmPermanentDelete(); }}
        >
          <p>删除后无法恢复；以后对账需要重新上传这份文件。</p>
          {deleteCandidate && <p style={{ color: tokens.color.text.secondary, wordBreak: "break-all" }}>{deleteCandidate.filename}</p>}
          {deleteError && <Alert role="alert" type="error" showIcon message={deleteError} />}
        </Modal>
      </>
    );
  }

  return (
    <section id="monthly-close-inbox" aria-label={showUploader ? "上传月结资料" : "月结资料收件记录"} style={{ border: compact ? 0 : `1px dashed ${tokens.anyu.color.sage}`, borderRadius: 12, padding: compact ? 0 : 18, background: tokens.anyu.color.shell }}>
      {showUploader && <>
        <input ref={inputRef} aria-label="统一上传月结资料" type="file" accept=".xls,.xlsx" multiple disabled={disabled || transferCount > 0} style={{ display: "none" }} onChange={(event) => { void receiveFiles(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
          <div>
            <div className="serif" style={{ fontSize: 20 }}>上传月结资料</div>
          </div>
          <Button style={touchControl} aria-label="统一上传原表" icon={<InboxOutlined />} loading={transferCount > 0} disabled={disabled} onClick={() => inputRef.current?.click()}>统一上传原表</Button>
        </div>
      </>}
      {localError && <Alert style={{ marginTop: 14 }} type="error" showIcon message={localError} />}
      {storedMessages.map((message) => (
        <Alert key={message} style={{ marginTop: 14 }} type="success" showIcon message={message} />
      ))}
      {items.length > 0 && (
        <Space direction="vertical" size={10} style={{ width: "100%", marginTop: 16 }}>
          {items.map((item) => {
            const busy = busyIds.has(item.item_id);
            const warn = item.status === "needs_review" || item.status === "failed";
            return (
              <div key={item.item_id} style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center", borderTop: `0.5px solid ${tokens.anyu.color.linen}`, paddingTop: 12 }}>
                <div style={{ minWidth: 180, flex: "1 1 260px" }}>
                  <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.filename}</div>
                  <div style={{ color: warn ? tokens.color.status.warn : tokens.color.text.tertiary, fontSize: 12, marginTop: 3 }}>{statusText(item)}</div>
                </div>
                <Select aria-label={`${item.filename}资料类型`} value={item.source_type === "utility_receipt" ? "utility_expense" : item.source_type ?? undefined} placeholder="请选择资料类型" options={MONTHLY_CLOSE_ACTIVE_SOURCE_OPTIONS} disabled={disabled || busy || item.status === "confirmed"} style={{ minWidth: 200, minHeight: 44, flex: "1 1 220px" }} onChange={(value) => void runItem(item, () => onSetSource(item.item_id, value))} />
                {item.confidence !== null && <span style={{ color: item.confidence < 0.8 ? tokens.anyu.color.clay : tokens.anyu.color.sage, fontSize: 12, whiteSpace: "nowrap" }}>{Math.round(item.confidence * 100)}% 把握</span>}
                {item.status === "received" && <Button style={touchControl} loading={busy} disabled={disabled} aria-label={`继续识别${item.filename}`} onClick={() => void runItem(item, () => onClassify(item.item_id))}>继续识别</Button>}
                {item.status === "failed" && <Button style={touchControl} loading={busy} disabled={disabled} aria-label={`${item.source_type ? "重试归档" : "重新识别"}${item.filename}`} onClick={() => void runItem(item, () => item.source_type ? onConfirm(item.item_id) : onClassify(item.item_id))}>{item.source_type ? "重试归档" : "重新识别"}</Button>}
                {item.status === "dismissed" && item.source_type && <Button style={touchControl} loading={busy} disabled={disabled} aria-label={`重新使用${item.filename}`} onClick={() => void runItem(item, () => onSetSource(item.item_id, item.source_type as string))}>重新使用</Button>}
                {item.status === "dismissed" && onPermanentDelete && <Button danger style={touchControl} loading={busy} disabled={disabled} aria-label={`永久删除${item.filename}`} onClick={() => { setDeleteError(null); setDeleteCandidate(item); }}>永久删除</Button>}
              </div>
            );
          })}
          {items.some((item) => item.status === "needs_review") && <Alert type="warning" showIcon message="请核对预选的资料类型；正确就直接确认归档，不正确请先修改" />}
          {(!compact || readyItems.length > 0) && <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button style={touchControl} aria-label={`确认归档 ${readyItems.length} 个文件`} type="primary" disabled={disabled || readyItems.length === 0 || busyIds.size > 0} onClick={() => void confirmReady()}>确认归档 {readyItems.length} 个文件</Button>
          </div>}
        </Space>
      )}
      <Modal
        title="永久删除这份文件？"
        open={deleteCandidate !== null}
        okText="永久删除"
        cancelText="取消"
        confirmLoading={Boolean(deleteCandidate && busyIds.has(deleteCandidate.item_id))}
        okButtonProps={{ danger: true }}
        maskClosable={false}
        onCancel={() => { setDeleteCandidate(null); setDeleteError(null); }}
        onOk={() => { void confirmPermanentDelete(); }}
      >
        <p>删除后无法恢复；以后对账需要重新上传这份文件。</p>
        {deleteCandidate && <p style={{ color: tokens.color.text.secondary, wordBreak: "break-all" }}>{deleteCandidate.filename}</p>}
        {deleteError && <Alert role="alert" type="error" showIcon message={deleteError} />}
      </Modal>
    </section>
  );
}
