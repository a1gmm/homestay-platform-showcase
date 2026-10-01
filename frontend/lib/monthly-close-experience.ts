import type { MonthlyCloseProjection, MonthlyCloseReceiptView, MonthlyCloseInboxItem } from "./monthly-close";

export function evidenceFilename(filename: string, index = 0) {
  if (!/^[a-f\d]{24,64}\.(png|jpe?g|webp)$/i.test(filename)) return filename;
  return `费用凭据图片 ${index + 1}`;
}

/** Count known identities, never filenames: different versions may have the same name. */
export function monthlyFiles(projection: MonthlyCloseProjection, receipts: MonthlyCloseReceiptView[] = [], inbox: MonthlyCloseInboxItem[] = []) {
  const files = new Map<string, { id: string; filename: string; documentId?: string; sourceId?: string; state: string }>();
  const knownReceipts = new Set<string>();
  for (const source of projection.sources) for (const doc of source.documents) {
    if (doc.receipt_id) knownReceipts.add(doc.receipt_id);
    files.set(doc.document_id, { id: doc.document_id, documentId: doc.document_id, filename: doc.filename || "月结文件", state: doc.storage_state === "stored" ? "原件已保存" : "文件接收未完成" });
  }
  for (const [index, source] of Array.from((projection.actor_role === "admin" ? projection.financial_case?.sources ?? [] : []).entries())) {
    const id = source.document_id || source.source_id;
    files.set(id, { id, documentId: source.document_id ?? undefined, sourceId: source.source_id,
      filename: evidenceFilename(source.filename, index), state: source.pending_count ? `原件已保存，${source.pending_count} 项待核实` : "原件已保存，请查看入账与核对结果" });
  }
  for (const item of inbox) {
    if (knownReceipts.has(item.item_id) || item.status === "confirmed" || item.status === "dismissed") continue;
    knownReceipts.add(item.item_id);
    files.set(`receipt:${item.item_id}`, { id: item.item_id, filename: item.filename, state: "已收到，待识别或确认类别" });
  }
  for (const receipt of receipts) {
    if (knownReceipts.has(receipt.receipt_id) || receipt.document_id && files.has(receipt.document_id)) continue;
    files.set(`receipt:${receipt.receipt_id}`, { id: receipt.receipt_id, filename: receipt.filename, state: "已收到，正在处理" });
  }
  return Array.from(files.values());
}

export function sourceReceiptState(projection: MonthlyCloseProjection, sourceType: string) {
  if (projection.actor_role !== "admin") return null;
  const sources = projection.financial_case?.sources.filter((source) => source.source_types.includes(sourceType)) ?? [];
  if (!sources.length) return null;
  if (sources.some((source) => source.pending_count > 0)) return "已有相关原件，内容待核实；无需重复上传";
  return "已有相关原件，需检查费用入账和凭据关联";
}
