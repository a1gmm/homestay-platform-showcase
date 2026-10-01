import { useCallback, useEffect, useRef, useState, type ComponentProps } from "react";
import { App, Button, Form, Input, InputNumber, Modal, Select, Typography, Spin } from "antd";

import { monthlyCloseApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import type { MonthlyCloseDocument, ServiceStatementAnalysis, ServiceStatementMapping } from "@/lib/monthly-close";
import { RecognitionSummary } from "./RecognitionSummary";

import { CleaningWorkLogWorkspace } from "./CleaningWorkLogWorkspace";

const FIELD_LABELS: Array<[string, string, boolean]> = [
  ["date", "服务日期列", true],
  ["room", "房间列", true],
  ["order", "订单号列", false],
  ["service", "服务类型列", false],
  ["quantity", "数量列", false],
  ["unit_price", "单价列", false],
  ["amount", "金额列", false],
];

function setColumn(
  mapping: ServiceStatementMapping,
  field: string,
  value: number | null,
): ServiceStatementMapping {
  const columns = { ...mapping.columns };
  if (value === null) delete columns[field];
  else columns[field] = value;
  return { ...mapping, columns };
}

function columnName(index: number) {
  let value = index + 1;
  let output = "";
  while (value > 0) {
    value -= 1;
    output = String.fromCharCode(65 + (value % 26)) + output;
    value = Math.floor(value / 26);
  }
  return output;
}

export function ServiceMappingReview({
  billingMonth,
  embedded = false,
  document,
  sourceType,
  disabled,
  role = "admin",
  onFinished,
}: {
  billingMonth: string;
  embedded?: boolean;
  document: MonthlyCloseDocument;
  sourceType?: string;
  disabled?: boolean;
  role?: string;
  onFinished: () => Promise<unknown>;
}) {
  const sourceName = sourceType === "cleaning_statement"
    ? "保洁打扫记录"
    : sourceType === "linen_statement"
      ? "布草／洗涤记录"
      : "保洁／布草记录";
  const { message } = App.useApp();
  const [analysis, setAnalysis] = useState<ServiceStatementAnalysis | null>(null);
  const [mapping, setMapping] = useState<ServiceStatementMapping | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [mappingModalReady, setMappingModalReady] = useState(false);
  const [analysisUnavailable, setAnalysisUnavailable] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  const requiresMappingCheck = analysisUnavailable || (analysis?.suggested_by === "administrator" && analysis.line_count === 0);
  const selectedSheet = analysis?.sheets?.find((sheet) => sheet.name === mapping?.sheet);
  const headerPreview = selectedSheet?.rows[mapping?.header_row ?? 0] ?? [];
  const columnOptions = Array.from(
    { length: Math.min(selectedSheet?.column_count ?? Math.max(headerPreview.length, 10), 100) },
    (_, index) => ({
      value: index,
      label: `${columnName(index)}${headerPreview[index] && headerPreview[index] !== "EMPTY" ? ` · ${headerPreview[index]}` : ""}`,
    }),
  );

  const analyze = useCallback(async () => {
    setAnalyzing(true);
    setMappingModalReady(false);
    setAnalysisUnavailable(false);
    setAdvancedOpen(false);
    try {
      const response = await monthlyCloseApi.analyzeServiceStatement(
        billingMonth,
        document.document_id,
      );
      setAnalysis(response.data);
      setMapping(response.data.mapping);
    } catch (error) {
      message.error(extractErrorMessage(error, "表格结构识别失败，请稍后重试"));
      setAnalysisUnavailable(true);
      setMapping({ sheet: "Sheet1", header_row: 0, columns: { date: 0, room: 1, amount: 2 } });
    } finally {
      setAnalyzing(false);
    }
  }, [billingMonth, document.document_id, message]);

  useEffect(() => { if (embedded) void analyze(); }, [embedded, analyze]);

  const confirm = async () => {
    if (!mapping || !mappingModalReady || analysis?.work_log) return;
    setConfirming(true);
    try {
      const checked = await monthlyCloseApi.analyzeServiceStatement(
        billingMonth,
        document.document_id,
        mapping,
      );
      if (checked.data.work_log || requiresMappingCheck) {
        setAnalysis(checked.data);
        setMapping(checked.data.mapping);
        setAnalysisUnavailable(false);
        return;
      }
      await monthlyCloseApi.confirmServiceStatement(
        billingMonth,
        document.document_id,
        checked.data.mapping,
      );
      message.success(`已保存 ${checked.data.line_count} 条供应商明细，请继续核对该份资料`);
      await onFinished();
      if (!mountedRef.current) return;
      setMappingModalReady(false);
      setMapping(null);
      setAnalysis(null);
    } catch (error) {
      if (!mountedRef.current) return;
      message.error(extractErrorMessage(error, "字段映射无法解析，请检查工作表和列号"));
    } finally {
      if (mountedRef.current) setConfirming(false);
    }
  };

  const ReviewSurface = embedded ? InlineReview : Modal;
  return (
    <>
      {embedded && analyzing && <div role="status" style={{ padding: "48px 0", display: "flex", gap: 12, alignItems: "center" }}><Spin />正在读取表格并核对系统记录…</div>}
      {!embedded && <Button size="small" disabled={disabled} loading={analyzing} onClick={() => void analyze()}>
        {analyzing ? "正在识别" : "查看识别结果"}
      </Button>}
      <ReviewSurface
        title={analysis?.work_log ? "按保洁表核对打扫记录" : `确认${sourceName}`}
        width={analysis?.work_log ? 960 : undefined}
        footer={analysis?.work_log ? null : undefined}
        open={mapping !== null}
        okText={requiresMappingCheck ? "校验读取设置" : "识别正确，开始核对"}
        cancelText="稍后处理"
        confirmLoading={confirming}
        okButtonProps={{
          disabled: !mappingModalReady
            || (requiresMappingCheck && (!advancedOpen || role !== "admin"))
            || !mapping?.sheet.trim()
            || mapping.columns.date === undefined
            || mapping.columns.room === undefined
            || (mapping.columns.amount === undefined
              && (mapping.columns.quantity === undefined || mapping.columns.unit_price === undefined)),
        }}
        onOk={() => void confirm()}
        onCancel={() => {
          setMappingModalReady(false);
          setAnalysisUnavailable(false);
          setAdvancedOpen(false);
          setMapping(null);
          setAnalysis(null);
        }}
        afterOpenChange={setMappingModalReady}
      >
        {analysis?.work_log && <CleaningWorkLogWorkspace key={`${billingMonth}-${document.document_id}`} billingMonth={billingMonth} documentId={document.document_id} filename={document.filename} initial={analysis.work_log} role={role} onFinished={onFinished} />}
        {mapping && !analysis?.work_log && (
          <RecognitionSummary
            documentName={document.filename}
            sourceLabel={sourceName}
            facts={analysis && !requiresMappingCheck ? [
              { label: "识别到", value: `${analysis.line_count} 条` },
              { label: "金额合计", value: `¥${analysis.total_amount}` },
            ] : []}
            warning={analysisUnavailable
              ? "系统没能自动读懂这份表格。文件已经保存，请让管理员确认一次读取方式。"
              : analysis?.suggested_by === "administrator"
                ? "系统没有把握判断表格结构，请核对记录数和金额；如不正确，请让管理员调整读取方式。"
                : undefined}
            note={analysis?.suggested_by === "remembered"
              ? "已沿用上月确认过的读取方式。确认后系统会重新校验每一行，确认前不会写入服务明细。"
              : "确认后系统会重新校验每一行，确认前不会写入服务明细。"}
            isAdmin={role === "admin"}
            onAdvancedChange={setAdvancedOpen}
            advanced={<Form layout="vertical">
              <Form.Item label="工作表名称" htmlFor="service-mapping-sheet" required>
                {analysis?.sheets?.length ? (
                  <Select
                    aria-label="工作表名称"
                    value={mapping.sheet}
                    options={analysis.sheets.map((sheet) => ({ value: sheet.name, label: `${sheet.name}（${sheet.row_count} 行）` }))}
                    onChange={(value) => setMapping({ ...mapping, sheet: value, header_row: 0 })}
                  />
                ) : (
                  <Input id="service-mapping-sheet" value={mapping.sheet} onChange={(event) => setMapping({ ...mapping, sheet: event.target.value })} />
                )}
              </Form.Item>
              <Form.Item label="表头在第几行" htmlFor="service-mapping-header-row" required>
                <InputNumber id="service-mapping-header-row" min={1} max={30} value={mapping.header_row + 1} onChange={(value) => setMapping({ ...mapping, header_row: Math.max((value ?? 1) - 1, 0) })} />
              </Form.Item>
              <Typography.Text strong>字段所在列</Typography.Text>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12, marginTop: 10 }}>
                {FIELD_LABELS.map(([field, label, required]) => (
                  <Form.Item key={field} label={label} htmlFor={`service-mapping-${field}`} required={required} style={{ marginBottom: 0 }}>
                    <Select
                      id={`service-mapping-${field}`}
                      allowClear={!required}
                      placeholder={required ? "必填" : "可选"}
                      value={mapping.columns[field]}
                      options={columnOptions}
                      onChange={(value) => setMapping(setColumn(mapping, field, value ?? null))}
                      onClear={() => setMapping(setColumn(mapping, field, null))}
                      style={{ width: "100%" }}
                    />
                  </Form.Item>
                ))}
              </div>
            </Form>}
          />
        )}
      </ReviewSurface>
    </>
  );
}

function InlineReview({ open, children, footer, okText, onOk, confirmLoading, okButtonProps, afterOpenChange }: ComponentProps<typeof Modal>) {
  useEffect(() => { afterOpenChange?.(!!open); }, [open, afterOpenChange]);
  if (!open) return null;
  return <>{children}{footer !== null && <Button type="primary" size="large" loading={confirmLoading} {...okButtonProps} onClick={onOk} style={{ marginTop: 20 }}>{okText}</Button>}</>;
}
