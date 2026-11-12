import type { MonthlyCloseProjection, MonthlyCloseReceiptView, MonthlyCloseInboxItem } from "./monthly-close";

export function evidenceFilename(filename: string, index = 0) {
  if (!/^[a-f\d]{24,64}\.(png|jpe?g|webp)$/i.test(filename)) return filename;
  return `费用凭据图片 ${index + 1}`;
}

export function originalProgress(source: NonNullable<MonthlyCloseProjection['financial_case']>['sources'][number]) {
  if (source.pending_count) return `有 ${source.pending_count} 项待核实，请查看具体问题`;
  if (source.receipt_confirmation) return `平台账单已核对；${source.receipt_confirmation.amount} 元到账已人工确认，银行流水另核`;
  if (source.reconciliation_state === 'matched' && source.kind === 'cleaning') return `${source.reconciled_record_count ?? source.fact_count} 次打扫记录与续住费用已核对`;
  if (source.state === 'matched' || source.state === 'posted') return source.kind === 'ota' ? '平台账单与当前订单已核对；实际到账另核' : '已对应现有账目，无需重复记账';
  return '原件已保存，请查看入账与核对结果';
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
      filename: evidenceFilename(source.filename, index), state: originalProgress(source) });
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
  if (sources.every(source => source.state === 'matched' || source.state === 'posted')) return '原件已对应现有账目；公司实际成本与付款凭据另行核对';
  return "已有相关原件，需检查费用入账和凭据关联";
}
