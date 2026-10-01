import { useRef, useState } from "react";
import { Alert, Button, Input, Radio } from "antd";
import { monthlyCloseApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import type { CleaningWorkComparison, CleaningWorkResolutionPreview, CleaningWorkResolutionSelection } from "@/lib/monthly-close";
import styles from "./CleaningWorkLogWorkspace.module.css";

export const decisionLabels = {
  exclude_system: "不计入这份表的核对",
  count_once: "重复填写，只算一次",
  accept_table: "按原表次数保留",
};

export function CleaningWorkResolutionReview({ item, billingMonth, documentId, onSaved, onBack }: {
  item: CleaningWorkComparison["differences"][number]; billingMonth: string; documentId: string;
  onSaved: (report: CleaningWorkComparison) => Promise<void>; onBack: () => void;
}) {
  const [decision, setDecision] = useState<CleaningWorkResolutionSelection["decision"] | null>(null);
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<CleaningWorkResolutionPreview | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const busy = useRef(false);
  const requestId = useRef("");
  const submit = async () => {
    if (busy.current || !decision || reason.trim().length < 2) return;
    busy.current = true; setPending(true); setError("");
    try {
      if (preview) {
        const { data } = await monthlyCloseApi.confirmCleaningResolution(billingMonth, documentId, preview, requestId.current);
        await onSaved(data.comparison);
      } else {
        const { data } = await monthlyCloseApi.previewCleaningResolution(billingMonth, documentId, { service_date: item.service_date, room_ref: item.room_ref, service_type: item.service_type, decision, reason: reason.trim() });
        requestId.current = crypto.randomUUID(); setPreview(data); setUncertain(false);
      }
    } catch (cause) {
      const status = (cause as { response?: { status?: number } }).response?.status;
      const unknown = !!preview && (!status || status >= 500);
      setUncertain(unknown);
      if (!unknown) setPreview(null);
      setError(extractErrorMessage(cause, unknown ? "暂未收到保存结果，请重试确认；同一次处理不会重复保存。" : "处理未完成，请返回列表刷新记录后重试。"));
    } finally { busy.current = false; setPending(false); }
  };
  const evidence = preview?.item ?? item;
  return <section aria-label="核实这条打扫差异" aria-busy={pending} className={styles.workspace}>
    <Button className={styles.backButton} disabled={pending || uncertain} onClick={onBack}>返回差异列表</Button>
    <div className={styles.notice}>
      <strong>{item.service_date} · {item.room_ref} · {item.service_type === "instay_cleaning" ? "续住打扫" : "正常打扫"}</strong>
      <p>原表 {evidence.table_count} 次 · 系统 {evidence.system_count} 条。请核实实际情况后选择处理方式。</p>
    </div>
    <div className={styles.evidenceGrid}>
      <section><h3>原表依据</h3>{evidence.source_entries?.length ? evidence.source_entries.map((row, i) => <p key={i}>{row.source_sheet} · 第 {row.source_row} 行 · 房号 {row.room_ref}</p>) : <p>原表没有这一天、房间和类型的记录。</p>}</section>
      <section><h3>系统依据</h3>{evidence.system_evidence?.length ? evidence.system_evidence.map((row) => <p key={row.record_id}><span>{row.kind === "history" ? `历史记录 ${row.quantity ?? 1} 次` : row.completed ? "已完成" : "缺少完成依据"}</span><small>{row.record_id}</small></p>) : <p>系统暂无对应记录。</p>}</section>
    </div>
    <fieldset className={styles.decisions} disabled={pending || !!preview}>
      <legend>核实后的处理方式</legend>
      <Radio.Group value={decision} onChange={(event) => setDecision(event.target.value)}>
        {evidence.table_count === 0 ? <Radio value="exclude_system">{decisionLabels.exclude_system}（原系统记录保留）</Radio> : <>
          {evidence.table_count > 1 && <Radio value="count_once">{decisionLabels.count_once}</Radio>}
          <Radio value="accept_table">{evidence.table_count > 1 ? `确实打扫 ${evidence.table_count} 次，按原表次数保留` : "按原表保留一次"}</Radio>
        </>}
      </Radio.Group>
      <label htmlFor="cleaning-resolution-reason">核实说明</label>
      <Input.TextArea id="cleaning-resolution-reason" value={reason} onChange={(event) => setReason(event.target.value)} disabled={pending || !!preview} maxLength={1000} autoSize={{ minRows: 3, maxRows: 6 }} placeholder="例如：与保洁员核实，原表第 7、8 行重复填写，实际只打扫一次。" />
    </fieldset>
    {error && <Alert role="alert" type="error" showIcon message={error} />}
    {preview && <div className={styles.notice} role="status"><strong>本次确认：{preview.confirmed_count === 0 ? "不计入这份表的核对" : `保留 ${preview.confirmed_count} 次打扫`}</strong><p>保存核实说明、原表依据和管理员信息。确认后重新核对，处理结果可在“已处理”中查看。</p></div>}
    <footer className={styles.footer}>
      <p role="status">{pending ? preview ? "正在保存核实结果，请稍候…" : "正在检查最新依据并生成预览…" : "仅处理当前差异，保留原始表格和系统任务。"}</p>
      <div className={styles.actions}>
        {preview && <Button disabled={pending || uncertain} onClick={() => setPreview(null)}>修改处理方式</Button>}
        <Button aria-label={preview ? uncertain ? "重试确认处理" : "确认处理并重新核对" : "预览处理结果"} type="primary" size="large" loading={pending} disabled={!decision || reason.trim().length < 2} onClick={() => void submit()}>{preview ? uncertain ? "重试确认处理" : "确认处理并重新核对" : "预览处理结果"}</Button>
      </div>
    </footer>
  </section>;
}
