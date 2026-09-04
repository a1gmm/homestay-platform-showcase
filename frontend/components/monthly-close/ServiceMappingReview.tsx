import { useState } from "react";
import { Alert, Button, Form, Input, InputNumber, Modal, Select, Space, Typography, message } from "antd";

import { monthlyCloseApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import type { MonthlyCloseDocument, ServiceStatementAnalysis, ServiceStatementMapping } from "@/lib/monthly-close";

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
  document,
  disabled,
  onFinished,
}: {
  billingMonth: string;
  document: MonthlyCloseDocument;
  disabled?: boolean;
  onFinished: () => Promise<unknown>;
}) {
  const [analysis, setAnalysis] = useState<ServiceStatementAnalysis | null>(null);
  const [mapping, setMapping] = useState<ServiceStatementMapping | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const selectedSheet = analysis?.sheets?.find((sheet) => sheet.name === mapping?.sheet);
  const headerPreview = selectedSheet?.rows[mapping?.header_row ?? 0] ?? [];
  const columnOptions = Array.from(
    { length: Math.min(selectedSheet?.column_count ?? Math.max(headerPreview.length, 10), 100) },
    (_, index) => ({
      value: index,
      label: `${columnName(index)}${headerPreview[index] && headerPreview[index] !== "EMPTY" ? ` · ${headerPreview[index]}` : ""}`,
    }),
  );

  const analyze = async () => {
    setAnalyzing(true);
    try {
      const response = await monthlyCloseApi.analyzeServiceStatement(
        billingMonth,
        document.document_id,
      );
      setAnalysis(response.data);
      setMapping(response.data.mapping);
    } catch (error) {
      message.error(extractErrorMessage(error, "表格结构识别失败，请稍后重试"));
      setMapping({ sheet: "Sheet1", header_row: 0, columns: { date: 0, room: 1, amount: 2 } });
    } finally {
      setAnalyzing(false);
    }
  };

  const confirm = async () => {
    if (!mapping) return;
    setConfirming(true);
    try {
      const checked = await monthlyCloseApi.analyzeServiceStatement(
        billingMonth,
        document.document_id,
        mapping,
      );
      await monthlyCloseApi.confirmServiceStatement(
        billingMonth,
        document.document_id,
        checked.data.mapping,
      );
      message.success(`已按确认结构导入 ${checked.data.line_count} 条明细`);
      setMapping(null);
      setAnalysis(null);
      await onFinished();
    } catch (error) {
      message.error(extractErrorMessage(error, "字段映射无法解析，请检查工作表和列号"));
    } finally {
      setConfirming(false);
    }
  };

  return (
    <>
      <Button size="small" disabled={disabled} loading={analyzing} onClick={() => void analyze()}>
        识别并确认表格结构
      </Button>
      <Modal
        title="复核保洁／布草表格结构"
        open={mapping !== null}
        okText="确认结构并导入"
        cancelText="稍后处理"
        confirmLoading={confirming}
        okButtonProps={{
          disabled: !mapping?.sheet.trim()
            || mapping.columns.date === undefined
            || mapping.columns.room === undefined
            || (mapping.columns.amount === undefined
              && (mapping.columns.quantity === undefined || mapping.columns.unit_price === undefined)),
        }}
        onOk={() => void confirm()}
        onCancel={() => { setMapping(null); setAnalysis(null); }}
      >
        {mapping && (
          <Space direction="vertical" size={16} style={{ width: "100%" }}>
            <Alert
              type={analysis ? "info" : "warning"}
              showIcon
              message={analysis
                ? analysis.suggested_by === "administrator"
                  ? "智能识别暂不可用，已读取原表供人工确认"
                  : `${analysis.suggested_by === "remembered" ? "已沿用上月确认结构，" : ""}识别到 ${analysis.line_count} 条，合计 ¥${analysis.total_amount}`
                : "自动识别暂不可用，可根据原表手工填写"}
              description="请直接按 A、B、C 列和真实表头确认。系统会重新解析并校验金额；本页确认前不会写入服务明细。"
            />
            <Form layout="vertical">
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
            </Form>
          </Space>
        )}
      </Modal>
    </>
  );
}
