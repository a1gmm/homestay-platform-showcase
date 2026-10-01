import { useRef, useState } from "react";
import { Alert, Button, Table, Tabs } from "antd";
import { monthlyCloseApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import type { CleaningWorkComparison, CleaningWorkPreview } from "@/lib/monthly-close";
import { CleaningWorkResolutionReview, decisionLabels } from "./CleaningWorkResolutionReview";
import styles from "./CleaningWorkLogWorkspace.module.css";

const statusLabels: Record<string, string> = {
  table_only: "表中有，系统缺少", system_only: "系统有，表中没有",
  unknown_room: "房号需要确认", duplicate: "同日同房存在重复记录", not_completed: "系统缺少完成依据",
};
const baseColumns = [
  { title: "日期", dataIndex: "service_date", width: 120 },
  { title: "房间", dataIndex: "room_ref", width: 100 },
  { title: "打扫类型", dataIndex: "service_type", width: 110, render: (value: string) => value === "instay_cleaning" ? "续住打扫" : "正常打扫" },
];
const evidenceColumn = { title: "原表位置", render: (_: unknown, row: { source_sheet: string; source_row: number }) => `${row.source_sheet} · 第 ${row.source_row} 行` };

export function CleaningWorkLogWorkspace({ billingMonth, documentId, filename, initial, role, onFinished }: {
  billingMonth: string; documentId: string; filename: string; initial: CleaningWorkComparison;
  role: string; onFinished: () => Promise<unknown>;
}) {
  const [report, setReport] = useState(initial);
  const [selected, setSelected] = useState<CleaningWorkComparison["differences"][number] | null>(null);
  const [preview, setPreview] = useState<CleaningWorkPreview | null>(null);
  const [tab, setTab] = useState(() => initial.differences.some((item) => ["table_only", "not_completed"].includes(item.status) && item.table_count === 1) ? "missing" : initial.differences.length ? "remaining" : "history");
  const [pending, setPending] = useState<"preview" | "confirm" | null>(null);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const busy = useRef(false);
  const requestId = useRef("");
  const [uncertain, setUncertain] = useState(false);
  const canAdd = (item: CleaningWorkComparison["differences"][number]) =>
    ["table_only", "not_completed"].includes(item.status) && item.table_count === 1;
  const missing = report.differences.filter(canAdd);
  const remaining = report.differences.filter((item) => !canAdd(item));
  const prepare = async () => {
    if (busy.current) return;
    busy.current = true; setPending("preview"); setError("");
    try {
      const { data } = await monthlyCloseApi.previewCleaningWork(billingMonth, documentId);
      requestId.current = crypto.randomUUID(); setUncertain(false);
      setReport(data.comparison); setPreview(data);
    } catch (cause) { setError(extractErrorMessage(cause, "预览未完成，请重试")); }
    finally { busy.current = false; setPending(null); }
  };
  const confirm = async () => {
    if (!preview || busy.current) return;
    busy.current = true; setPending("confirm"); setError("");
    try {
      const { data } = await monthlyCloseApi.confirmCleaningWork(billingMonth, documentId, preview.preview_hash, requestId.current);
      setReport(data.comparison); setPreview(null); setUncertain(false);
      setSuccess(`已补齐 ${data.added_count} 条历史打扫记录，剩余 ${data.comparison.differences.length} 项需要核实。`);
      setTab(data.comparison.differences.length ? "remaining" : "history");
      try { await onFinished(); } catch { setError("记录已保存，页面进度刷新失败。请刷新页面查看最新状态。"); }
    } catch (cause) {
      const status = (cause as { response?: { status?: number } }).response?.status;
      // Keep the same request ID for uncertain network/server outcomes.
      const retrySameBatch = !status || status >= 500;
      setUncertain(retrySameBatch);
      if (!retrySameBatch) setPreview(null);
      setError(extractErrorMessage(cause, retrySameBatch ? "暂未收到保存结果，请点击重试确认；同一批记录不会重复补齐。" : "记录状态已变化，请重新预览"));
    } finally { busy.current = false; setPending(null); }
  };
  const actionLabel = preview ? uncertain ? "重试确认这批记录" : `确认补齐 ${preview.add_count} 条历史记录` : `预览补齐 ${missing.length} 条记录`;
  const differenceRows = tab === "missing" ? missing : remaining;
  return <section className={styles.workspace} aria-label="保洁工作记录核对结果" aria-busy={pending !== null}>
    <header className={styles.header}>
      <p className={styles.eyebrow}>{billingMonth} · 保洁记录核对</p>
      <h2>{preview ? "确认这次要补齐的记录" : "按保洁表补齐打扫记录"}</h2>
      <p className={styles.filename}>{filename}</p>
      <p>按日期、房间和正常／续住打扫对应。以原表为准，忽略超出数量和金额。</p>
    </header>
    {error && <Alert type="error" showIcon message={error} role="alert" />}
    {success && <Alert type="success" showIcon message={success} role="status" />}
    {selected ? <CleaningWorkResolutionReview item={selected} billingMonth={billingMonth} documentId={documentId} onBack={() => setSelected(null)} onSaved={async (updated) => {
      setReport(updated); setSelected(null); setTab(updated.differences.length ? "remaining" : "resolved");
      setSuccess(`核实结果已保存，剩余 ${updated.differences.length} 项待处理。`);
      try { await onFinished(); } catch { setError("核实结果已保存，页面进度刷新失败，请刷新页面。"); }
    }} /> : <>
    <dl className={styles.summary}>
      <div><dt>表内记录</dt><dd>{report.record_count}<small>正常 {report.normal_count} · 续住 {report.instay_count}</small></dd></div>
      <div><dt>已对应</dt><dd>{report.matched_count}<small>核实后有效 {report.effective_record_count ?? report.record_count} 次</small></dd></div>
      <div><dt>可补齐</dt><dd>{missing.length}<small>缺少记录或完成依据</small></dd></div>
      <div><dt>需单独核实</dt><dd>{remaining.length}<small>重复、表外或房号问题</small></dd></div>
    </dl>
    {preview ? <>
      <div className={styles.notice}><strong>将补齐 {preview.add_count} 条历史打扫记录</strong><p>每条保存原表位置，可在“已补齐记录”中查看。补齐后仍有 {preview.remaining_count} 项需核实。当前房态、运营任务和费用不随此操作变更。</p></div>
      <Table size="middle" rowKey={(row) => `${row.service_date}-${row.room_ref}-${row.service_type}`} dataSource={preview.actions} columns={[...baseColumns, evidenceColumn]} scroll={{ x: 620 }} pagination={{ pageSize: 10, showSizeChanger: false, showTotal: (n) => `共 ${n} 条待补齐` }} />
    </> : <>
      <Tabs activeKey={tab} onChange={setTab} items={[
        { key: "missing", label: `待补齐 ${missing.length}` },
        { key: "remaining", label: `需核实 ${remaining.length}` },
        { key: "resolved", label: `已处理 ${report.resolutions?.filter((item) => item.state === "applied").length ?? 0}` },
        { key: "history", label: `已补齐记录 ${report.recorded_count ?? 0}` },
      ]} />
      {tab === "resolved" ? <Table size="middle" rowKey="resolution_id" dataSource={report.resolutions ?? []} columns={[...baseColumns,
        { title: "处理结果", render: (_: unknown, row: NonNullable<CleaningWorkComparison["resolutions"]>[number]) => <>{decisionLabels[row.decision]} · {row.confirmed_count} 次{row.state === "stale" && <p>依据已变化，原结论未采用</p>}</> },
        { title: "核实说明", dataIndex: "reason" },
        { title: "管理员 / 时间", render: (_: unknown, row: NonNullable<CleaningWorkComparison["resolutions"]>[number]) => <>{row.confirmed_by}<br />{new Date(row.created_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}</> },
      ]} scroll={{ x: 900 }} locale={{ emptyText: "确认处理差异后，核实说明和处理结果会保存在这里。" }} /> : tab === "history" ? <Table size="middle" rowKey="record_id" dataSource={report.records ?? []} columns={[...baseColumns, { title: "次数", dataIndex: "quantity", render: (value: number) => value ?? 1 }, evidenceColumn]} scroll={{ x: 620 }} pagination={{ pageSize: 10, showSizeChanger: false }} locale={{ emptyText: "尚未补齐历史记录，确认补齐后会保存在这里。" }} /> : <Table size="middle" rowKey={(row) => `${row.service_date}-${row.room_ref}-${row.service_type}-${row.source_rows.join(",")}`} dataSource={differenceRows} columns={[...baseColumns,
        { title: "核对结果", dataIndex: "status", render: (value: string) => statusLabels[value] ?? value },
        { title: "原表行", dataIndex: "source_rows", width: 100, render: (rows: number[]) => rows.join("、") || "—" },
        { title: "处理", key: "action", fixed: "right" as const, width: 140, render: (_: unknown, row: CleaningWorkComparison["differences"][number]) => role === "admin" && ["duplicate", "system_only"].includes(row.status) ? <Button style={{ minHeight: 44 }} onClick={() => { setSelected(row); setError(""); setSuccess(""); }}>核实并处理</Button> : row.status === "unknown_room" ? "请先核实原表房号" : "可直接补齐" },
      ]} scroll={{ x: 810 }} pagination={{ pageSize: 10, showSizeChanger: false, showTotal: (n) => `共 ${n} 项` }} locale={{ emptyText: tab === "missing" ? "没有可直接补齐的记录，请查看需核实项或已补齐记录。" : "没有需要单独核实的差异。" }} />}
    </>}
    <footer className={styles.footer}>
      <p role="status">{pending === "preview" ? "正在核对最新记录并生成补齐预览…" : pending === "confirm" ? "正在保存这批记录，请稍候…" : preview ? "确认后保存到本月历史打扫记录，并保留原表依据。" : role !== "admin" ? "只有管理员可以补齐记录。" : missing.length ? "先预览具体记录，再确认补齐。" : remaining.length ? "点击每条差异右侧的“核实并处理”，确认后保存。" : "这份保洁表已核对完成，可查看已处理结果和历史记录。"}</p>
      <div className={styles.actions}>
        {preview && <Button disabled={pending !== null || uncertain} onClick={() => setPreview(null)}>返回核对</Button>}
        {(preview || missing.length > 0) && <Button aria-label={actionLabel} type="primary" size="large" loading={pending !== null} disabled={role !== "admin" || (!preview && missing.length === 0) || (preview?.add_count === 0)} onClick={() => void (preview ? confirm() : prepare())}>
          {actionLabel}
        </Button>}
      </div>
    </footer>
    </>}
  </section>;
}
