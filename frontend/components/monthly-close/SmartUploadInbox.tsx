import { useRef, useState } from "react";
import { Alert, Button, Select, Space } from "antd";
import { InboxOutlined } from "@ant-design/icons";

import { extractErrorMessage } from "@/lib/api-errors";
import type { MonthlyCloseInboxItem } from "@/lib/monthly-close";
import { MONTHLY_CLOSE_SOURCE_LABELS } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";

const sourceOptions = Object.entries(MONTHLY_CLOSE_SOURCE_LABELS).map(([value, label]) => ({ value, label }));

function statusText(item: MonthlyCloseInboxItem) {
  if (item.status === "received") return "已收件，等待识别";
  if (item.status === "needs_review") return item.classification_reason || "识别把握不足，请确认资料类型";
  if (item.status === "classified") return item.classification_reason || "已识别，等待确认归档";
  if (item.status === "confirmed") return "已归档到本月资料";
  if (item.status === "dismissed") return "已忽略（原归档已撤销）";
  return item.last_error || "处理失败，请重试";
}

export function SmartUploadInbox({ items, disabled, onReceive, onClassify, onSetSource, onConfirm }: {
  items: MonthlyCloseInboxItem[];
  disabled?: boolean;
  onReceive: (file: File) => Promise<MonthlyCloseInboxItem>;
  onClassify: (itemId: string) => Promise<unknown>;
  onSetSource: (itemId: string, sourceType: string) => Promise<unknown>;
  onConfirm: (itemId: string) => Promise<unknown>;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [transferCount, setTransferCount] = useState(0);
  const [localError, setLocalError] = useState<string | null>(null);

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
    setTransferCount(files.length);
    for (const file of files) {
      try {
        const item = await onReceive(file);
        await onClassify(item.item_id);
      } catch (error) {
        setLocalError(extractErrorMessage(error, `“${file.name}”收件失败，请重试`));
      } finally {
        setTransferCount((current) => Math.max(0, current - 1));
      }
    }
  };

  const readyItems = items.filter((item) => item.status === "classified");
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

  return (
    <section id="monthly-close-inbox" aria-label="上传月结资料" style={{ border: `1px dashed ${tokens.anyu.color.sage}`, borderRadius: 12, padding: 18, background: tokens.anyu.color.shell }}>
      <input ref={inputRef} aria-label="统一上传月结资料" type="file" accept=".xls,.xlsx" multiple disabled={disabled || transferCount > 0} style={{ display: "none" }} onChange={(event) => { void receiveFiles(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
        <div>
          <div className="serif" style={{ fontSize: 20 }}>上传月结资料</div>
        </div>
        <Button aria-label="统一上传原表" icon={<InboxOutlined />} loading={transferCount > 0} disabled={disabled} onClick={() => inputRef.current?.click()}>统一上传原表</Button>
      </div>
      {localError && <Alert style={{ marginTop: 14 }} type="error" showIcon message={localError} />}
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
                <Select aria-label={`${item.filename}资料类型`} value={item.source_type ?? undefined} placeholder="请选择资料类型" options={sourceOptions} disabled={disabled || busy || item.status === "confirmed"} style={{ minWidth: 200, flex: "1 1 220px" }} onChange={(value) => void runItem(item, () => onSetSource(item.item_id, value))} />
                {item.confidence !== null && <span style={{ color: item.confidence < 0.8 ? tokens.anyu.color.clay : tokens.anyu.color.sage, fontSize: 12, whiteSpace: "nowrap" }}>{Math.round(item.confidence * 100)}% 把握</span>}
                {item.status === "received" && <Button loading={busy} disabled={disabled} aria-label={`继续识别${item.filename}`} onClick={() => void runItem(item, () => onClassify(item.item_id))}>继续识别</Button>}
                {item.status === "failed" && <Button loading={busy} disabled={disabled} aria-label={`${item.source_type ? "重试归档" : "重新识别"}${item.filename}`} onClick={() => void runItem(item, () => item.source_type ? onConfirm(item.item_id) : onClassify(item.item_id))}>{item.source_type ? "重试归档" : "重新识别"}</Button>}
                {item.status === "dismissed" && item.source_type && <Button loading={busy} disabled={disabled} aria-label={`重新使用${item.filename}`} onClick={() => void runItem(item, () => onSetSource(item.item_id, item.source_type as string))}>重新使用</Button>}
              </div>
            );
          })}
          {items.some((item) => item.status === "needs_review") && <Alert type="warning" showIcon message="有识别不确定的文件，请确认资料类型后再归档" />}
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button aria-label={`确认归档 ${readyItems.length} 个文件`} type="primary" disabled={disabled || readyItems.length === 0 || busyIds.size > 0} onClick={() => void confirmReady()}>确认归档 {readyItems.length} 个文件</Button>
          </div>
        </Space>
      )}
    </section>
  );
}
