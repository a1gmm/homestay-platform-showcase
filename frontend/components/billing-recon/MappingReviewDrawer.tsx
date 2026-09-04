"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Checkbox, Drawer, Input, InputNumber, Space, Table, Tag, Typography } from "antd";

import type {
  BillingReconConfirmInput, BillingReconFieldError, CellRef, MappingCoordinates, PlatformScope, WorkbookAnalysis,
} from "@/lib/billing-recon";

const { Text } = Typography;

const platformOptions: Array<{ value: PlatformScope; label: string }> = [
  { value: "ctrip_family", label: "携程系" }, { value: "meituan", label: "美团" },
  { value: "fliggy", label: "飞猪" }, { value: "douyin", label: "抖音" },
  { value: "tujia", label: "途家" }, { value: "all_ota", label: "未知或全部 OTA" },
];
const warningCopy: Record<string, string> = {
  NO_INDEPENDENT_TOTAL: "未找到独立汇总金额：核验强度会降低，但仍可确认继续对账。",
  SUMMARY_CELL_IS_DETAIL: "汇总单元格疑似明细行：请检查汇总工作表、行和列坐标。",
};

type Draft = {
  coordinates: MappingCoordinates;
  platform_scope: PlatformScope;
  remember_layout: boolean;
  rowTypeMapText: string;
};

function draftFor(analysis: WorkbookAnalysis): Draft {
  const coordinates = analysis.coordinates;
  return {
    coordinates: { ...coordinates, row_type_map: { ...coordinates.row_type_map }, summary_cell: coordinates.summary_cell && { ...coordinates.summary_cell } },
    platform_scope: analysis.platform_scope,
    remember_layout: true,
    rowTypeMapText: Object.entries(coordinates.row_type_map).map(([source, type]) => `${source}=${type}`).join("\n"),
  };
}

function columnLetter(index: number): string {
  let n = index + 1;
  let output = "";
  while (n > 0) {
    output = String.fromCharCode(65 + ((n - 1) % 26)) + output;
    n = Math.floor((n - 1) / 26);
  }
  return output;
}

function nonNegative(value: number | null): number { return Math.max(0, Math.trunc(value ?? 0)); }

function rowTypeMap(text: string): MappingCoordinates["row_type_map"] {
  return Object.fromEntries(text.split("\n").flatMap((line) => {
    const [source, type, ...remaining] = line.split("=");
    return source?.trim() && type?.trim() && remaining.length === 0
      ? [[source.trim(), type.trim() as "normal" | "refund" | "compensation"]]
      : [];
  }));
}

type ReviewError = Pick<BillingReconFieldError, "code" | "message" | "field">;

function errorField(error: ReviewError | undefined): string {
  if (error?.code === "FILE_INVALID" || error?.code === "FILE_CHANGED") return "file";
  return error?.field === "mapping_json" || !error?.field ? "mapping" : error.field;
}

function FieldError({ errors, field }: { errors: ReviewError[]; field: string }) {
  const matching = errors.filter((error) => errorField(error) === field);
  return matching.length > 0
    ? <div id={`${field}-error`} role="alert" style={{ color: "#cf1322", marginTop: 4 }}><ul aria-label={`${field} 错误`} style={{ margin: 0, paddingLeft: 18 }}>{matching.map((error, index) => <li key={`${error.code}-${index}`}>{error.message}</li>)}</ul></div>
    : null;
}

function FileErrorRegion({ errors }: { errors: ReviewError[] }) {
  return <div id="mapping-file" role="alert" aria-live="assertive" aria-label={`账单文件错误：${errors.map((error) => error.message).join("；")}`} tabIndex={-1} style={{ color: "#cf1322" }}><Text strong>账单文件需要重新确认</Text><ul style={{ margin: "4px 0", paddingLeft: 18 }}>{errors.map((error, index) => <li key={`${error.code}-${index}`}>{error.message}</li>)}</ul><div>请重新选择原始账单文件后重新分析，再确认。</div></div>;
}

export function MappingReviewDrawer({
  open, analysis, analysisCurrent, loading, error, onClose, onConfirm, onMappingChange, onReanalyze,
}: {
  open: boolean;
  analysis: WorkbookAnalysis | null;
  analysisCurrent: boolean;
  loading: boolean;
  error: BillingReconFieldError | null;
  onClose: () => void;
  onConfirm: (input: Omit<BillingReconConfirmInput, "file" | "file_fingerprint">) => void;
  onMappingChange: (coordinates: MappingCoordinates) => void;
  onReanalyze: (coordinates: MappingCoordinates) => void;
}) {
  const identity = analysis ? `${analysis.file_fingerprint}:${analysis.layout_signature}` : null;
  const [draft, setDraft] = useState<Draft | null>(() => analysis ? draftFor(analysis) : null);
  const identityRef = useRef(identity);
  const confirmingRef = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const reviewErrors = useMemo<ReviewError[]>(
    () => [...(analysisCurrent ? (analysis?.errors ?? []) : []), ...(error ? [error] : [])],
    [analysis?.errors, analysisCurrent, error],
  );
  const mappingErrors = reviewErrors.filter((reviewError) => errorField(reviewError) === "mapping");
  const fileErrors = reviewErrors.filter((reviewError) => errorField(reviewError) === "file");
  const recoveryErrors = reviewErrors.filter((reviewError) => errorField(reviewError) !== "file");
  const focusError = error ?? reviewErrors[0];
  const focusTarget = !analysis || !draft
    ? errorField(focusError) === "file" ? "file" : "mapping"
    : errorField(focusError);

  // Only an analysis identity replaces operator edits. Loading/error changes are deliberately ignored.
  useEffect(() => {
    if (identity && analysis && identity !== identityRef.current) {
      setDraft(draftFor(analysis));
      identityRef.current = identity;
      confirmingRef.current = false;
      setSubmitting(false);
    }
  }, [analysis, identity]);
  useEffect(() => {
    if (!loading) { confirmingRef.current = false; setSubmitting(false); }
  }, [loading]);
  useEffect(() => {
    if (!open || !focusError) return;
    // Drawer applies its own initial focus after children mount; defer once so the
    // server-reported mapping target remains the active, keyboard-visible control.
    const timer = window.setTimeout(() => document.getElementById(`mapping-${focusTarget}`)?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [focusError, focusTarget, open]);

  const maxColumn = useMemo(() => draft ? Math.max(
    25, draft.coordinates.col_order_no, draft.coordinates.col_guest, draft.coordinates.col_checkin,
    draft.coordinates.col_checkout, draft.coordinates.col_amount, draft.coordinates.col_row_type ?? 0,
    draft.coordinates.summary_cell?.col ?? 0,
  ) + 12 : 25, [draft]);
  const columns = useMemo(() => Array.from({ length: maxColumn + 1 }, (_, col) => ({ value: String(col), label: `${columnLetter(col)} (${col})` })), [maxColumn]);
  const commitMappingDraft = (next: Draft) => {
    setDraft(next);
    const coordinates = { ...next.coordinates, row_type_map: rowTypeMap(next.rowTypeMapText) };
    onMappingChange(coordinates);
    onReanalyze(coordinates);
  };
  const setCoordinates = (patch: Partial<MappingCoordinates>) => {
    if (!draft) return;
    commitMappingDraft({ ...draft, coordinates: { ...draft.coordinates, ...patch } });
  };
  const setSummary = (patch: Partial<CellRef>) => {
    if (!draft) return;
    commitMappingDraft({ ...draft, coordinates: { ...draft.coordinates, summary_cell: { ...(draft.coordinates.summary_cell ?? { sheet: draft.coordinates.sheet, row: 0, col: 0 }), ...patch } } });
  };
  const setRowTypeMapText = (rowTypeMapText: string) => {
    if (!draft) return;
    commitMappingDraft({ ...draft, rowTypeMapText });
  };
  const invalid = (field: string) => reviewErrors.some((reviewError) => errorField(reviewError) === field);
  const fieldProps = (field: string) => ({ id: `mapping-${field}`, "aria-invalid": invalid(field) || undefined, "aria-describedby": invalid(field) ? `${field}-error` : undefined });
  const confirm = () => {
    if (!draft || !analysisCurrent || loading || confirmingRef.current) return;
    confirmingRef.current = true;
    setSubmitting(true);
    onConfirm({ coordinates: { ...draft.coordinates, row_type_map: rowTypeMap(draft.rowTypeMapText) }, platform_scope: draft.platform_scope, remember_layout: draft.remember_layout });
  };

  return <Drawer open={open} onClose={onClose} placement="right" width={560} title="确认账单字段映射" destroyOnHidden={false}
    footer={<Space style={{ display: "flex", justifyContent: "flex-end" }}><Button aria-label="关闭" onClick={onClose}>关闭</Button><Button aria-label="确认并开始对账" type="primary" loading={loading || submitting} disabled={loading || submitting || !draft || !analysisCurrent} onClick={confirm}>确认并开始对账</Button></Space>}>
    {!analysis || !draft ? <Space direction="vertical" size="middle" style={{ display: "flex" }}>{fileErrors.length > 0 && <FileErrorRegion errors={fileErrors} />}{recoveryErrors.length > 0 && <div id="mapping-mapping" role="alert" aria-live="assertive" aria-label={`字段映射错误：${recoveryErrors.map((reviewError) => reviewError.message).join("；")}`} tabIndex={-1} style={{ color: "#cf1322" }}><Text strong>账单分析需要重新处理</Text><ul style={{ margin: "4px 0", paddingLeft: 18 }}>{recoveryErrors.map((reviewError, index) => <li key={`${reviewError.code}-${index}`}>{reviewError.message}</li>)}</ul><div>请修正问题后重新选择账单并重新分析。</div></div>}<Alert type="info" showIcon message="请先完成账单分析" /></Space> : <Space direction="vertical" size="middle" style={{ display: "flex" }}>
      {analysis.template_hit && <Alert type="success" showIcon message="已命中已确认的布局模板" />}
      {analysisCurrent
        ? analysis.warnings.map((warning) => <Alert key={warning} type="warning" showIcon message={`${warning}：${warningCopy[warning] ?? warning}`} />)
        : <Alert type="warning" showIcon message="当前解析指标已过期，请修改映射或等待重新分析成功。" />}
      {fileErrors.length > 0 && <FileErrorRegion errors={fileErrors} />}

      <section aria-labelledby="mapping-controls-heading" aria-describedby={mappingErrors.length > 0 ? "mapping-error" : undefined}><Text strong id="mapping-controls-heading">字段映射</Text>
        {mappingErrors.length > 0 && <div id="mapping-mapping" role="alert" aria-live="assertive" aria-label={mappingErrors.map((reviewError) => reviewError.message).join("；")} tabIndex={-1} style={{ color: "#cf1322", marginTop: 8 }}><div id="mapping-error">{mappingErrors.map((reviewError) => reviewError.message).join("；")}</div></div>}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 10 }}>
          <label>工作表<Input aria-label="工作表" value={draft.coordinates.sheet} onChange={(event) => setCoordinates({ sheet: event.target.value })} {...fieldProps("sheet")} /><FieldError errors={reviewErrors} field="sheet" /></label>
          <label>表头行（从 0 开始）<InputNumber aria-label="表头行" min={0} value={draft.coordinates.header_row} onChange={(value) => setCoordinates({ header_row: nonNegative(value) })} {...fieldProps("header_row")} style={{ width: "100%" }} /><FieldError errors={reviewErrors} field="header_row" /></label>
          <label>平台范围<select aria-label="平台范围" value={draft.platform_scope} onChange={(event) => setDraft((current) => current && ({ ...current, platform_scope: event.target.value as PlatformScope }))} {...fieldProps("platform_scope")}><option value="" disabled>选择平台范围</option>{platformOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select><FieldError errors={reviewErrors} field="platform_scope" /></label>
          <span aria-hidden="true" />
          {([ ["订单号列", "col_order_no", "订单号"], ["客人列", "col_guest", "客人"], ["入住列", "col_checkin", "入住日期"], ["离店列", "col_checkout", "离店日期"], ["金额列", "col_amount", "金额"] ] as const).map(([label, field, role]) => <label key={field}>{label}（{role}）<select aria-label={label} value={String(draft.coordinates[field])} onChange={(event) => setCoordinates({ [field]: Number(event.target.value) })} {...fieldProps(field)}>{columns.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select><FieldError errors={reviewErrors} field={field} /></label>)}
          <label>行类型列（可选）<select aria-label="行类型列" value={draft.coordinates.col_row_type == null ? "none" : String(draft.coordinates.col_row_type)} onChange={(event) => setCoordinates({ col_row_type: event.target.value === "none" ? null : Number(event.target.value) })} {...fieldProps("col_row_type")}><option value="none">不使用行类型列</option>{columns.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select><FieldError errors={reviewErrors} field="col_row_type" /></label>
        </div>
        <label style={{ display: "block", marginTop: 12 }}>行类型映射<Input.TextArea aria-label="行类型映射" value={draft.rowTypeMapText} onChange={(event) => setRowTypeMapText(event.target.value)} placeholder="正常订单=normal" rows={3} {...fieldProps("row_type_map")} /><Text type="secondary">每行“账单文本=normal、refund 或 compensation”。</Text><FieldError errors={reviewErrors} field="row_type_map" /></label>
      </section>

      <section aria-labelledby="summary-controls-heading"><Text strong id="summary-controls-heading">独立汇总金额（可选）</Text>
        <Checkbox checked={!!draft.coordinates.summary_cell} onChange={(event) => setCoordinates({ summary_cell: event.target.checked ? { sheet: draft.coordinates.sheet, row: 0, col: 0 } : null })}>使用独立汇总单元格</Checkbox>
        {draft.coordinates.summary_cell && <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginTop: 10 }}>
          <label>汇总工作表<Input aria-label="汇总工作表" value={draft.coordinates.summary_cell.sheet} onChange={(event) => setSummary({ sheet: event.target.value })} {...fieldProps("summary_cell")} /></label>
          <label>汇总行<InputNumber aria-label="汇总行" min={0} value={draft.coordinates.summary_cell.row} onChange={(value) => setSummary({ row: nonNegative(value) })} id="mapping-summary_cell-row" aria-invalid={invalid("summary_cell") || undefined} aria-describedby={invalid("summary_cell") ? "summary_cell-error" : undefined} style={{ width: "100%" }} /></label>
          <label>汇总列<select aria-label="汇总列" value={String(draft.coordinates.summary_cell.col)} onChange={(event) => setSummary({ col: Number(event.target.value) })} id="mapping-summary_cell-col" aria-invalid={invalid("summary_cell") || undefined} aria-describedby={invalid("summary_cell") ? "summary_cell-error" : undefined}>{columns.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>
          <FieldError errors={reviewErrors} field="summary_cell" />
        </div>}
      </section>
      <Checkbox aria-label="记住此布局" checked={draft.remember_layout} onChange={(event) => setDraft((current) => current && ({ ...current, remember_layout: event.target.checked }))}>记住此布局</Checkbox>
      {analysisCurrent && <><section aria-labelledby="quality-heading"><Text strong id="quality-heading">解析质量</Text><div style={{ marginTop: 8 }}><Tag color="blue">已解析 {analysis.quality.parsed_row_count} 行</Tag><Tag>订单号 {Math.round(analysis.quality.order_no_parse_ratio * 100)}%</Tag><Tag>金额 {Math.round(analysis.quality.amount_parse_ratio * 100)}%</Tag><Tag>离店日期 {Math.round(analysis.quality.checkout_parse_ratio * 100)}%</Tag> <Tag>明细合计 {analysis.quality.computed_total}</Tag><Tag color={analysis.quality.independent_total_verified ? "success" : "warning"}>{analysis.quality.independent_total_verified ? "已独立核验汇总" : "未独立核验汇总"}</Tag></div>{Object.entries(analysis.field_confidence).length > 0 && <Text type="secondary">AI 字段置信度：{Object.entries(analysis.field_confidence).map(([field, confidence]) => `${field} ${Math.round(confidence * 100)}%`).join(" · ")}</Text>}</section>
      <section aria-labelledby="preview-heading"><Text strong id="preview-heading">脱敏预览</Text><Table size="small" pagination={false} rowKey="order_no" dataSource={analysis.preview} columns={[
        { title: "订单号", dataIndex: "order_no", render: (value) => value ?? "—" }, { title: "客人", dataIndex: "guest", render: (value) => value ?? "—" }, { title: "入住", dataIndex: "checkin", render: (value) => value ?? "—" }, { title: "离店", dataIndex: "checkout", render: (value) => value ?? "—" }, { title: "金额", dataIndex: "amount", render: (value) => value ?? "—" }, { title: "类型", dataIndex: "row_type", render: (value) => value ?? "—" },
      ]} /></section></>}
    </Space>}
  </Drawer>;
}
