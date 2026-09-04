import { useState } from "react";
import { Alert, Button, Form, Input, InputNumber, Modal, Select, Space, message } from "antd";

import { monthlyCloseApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import type { MonthlyCloseDocument, UtilityStatementAnalysis, UtilityStatementMapping } from "@/lib/monthly-close";

const OPTIONAL_FIELDS = [
  ["floor", "楼层列"],
  ["room", "房间列"],
  ["customer", "客户列"],
  ["category", "费用科目列"],
  ["summary", "摘要／备注列"],
] as const;

function setColumn(mapping: UtilityStatementMapping, field: string, value: number | null) {
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

export function UtilityMappingReview({
  billingMonth,
  document,
  sourceType,
  disabled,
  onFinished,
}: {
  billingMonth: string;
  document: MonthlyCloseDocument;
  sourceType: string;
  disabled?: boolean;
  onFinished: () => Promise<unknown>;
}) {
  const role = sourceType === "utility_receipt" ? "receipt" : "expense";
  const amountField = role === "receipt" ? "receipt_amount" : "expense_amount";
  const [analysis, setAnalysis] = useState<UtilityStatementAnalysis | null>(null);
  const [mapping, setMapping] = useState<UtilityStatementMapping | null>(null);
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
      const response = await monthlyCloseApi.analyzeUtilityStatement(
        billingMonth,
        document.document_id,
      );
      setAnalysis(response.data);
      setMapping(response.data.mapping);
    } catch (error) {
      message.error(extractErrorMessage(error, "自动识别失败，请人工确认列号"));
      setMapping({
        role,
        sheet: "Sheet1",
        header_row: 0,
        columns: { date: 0, [amountField]: 1 },
      });
    } finally {
      setAnalyzing(false);
    }
  };

  const confirm = async () => {
    if (!mapping) return;
    setConfirming(true);
    try {
      const checked = await monthlyCloseApi.analyzeUtilityStatement(
        billingMonth,
        document.document_id,
        mapping,
      );
      await monthlyCloseApi.confirmUtilityStatement(
        billingMonth,
        document.document_id,
        checked.data.mapping,
      );
      message.success("已记住该水电表格格式");
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
        智能检查格式
      </Button>
      <Modal
        title={role === "receipt" ? "复核水电已收明细结构" : "复核水电费用明细结构"}
        open={mapping !== null}
        okText="确认并记住格式"
        cancelText="稍后处理"
        confirmLoading={confirming}
        okButtonProps={{
          disabled: !mapping?.sheet.trim()
            || mapping.columns.date === undefined
            || mapping.columns[amountField] === undefined,
        }}
        onOk={() => void confirm()}
        onCancel={() => { setMapping(null); setAnalysis(null); }}
      >
        {mapping && (
          <Space direction="vertical" size={14} style={{ width: "100%" }}>
            <Alert
              type={analysis ? "info" : "warning"}
              showIcon
              message={analysis
                ? analysis.suggested_by === "administrator"
                  ? "智能识别暂不可用，已读取原表供人工确认"
                  : `${analysis.suggested_by === "remembered" ? "已沿用上月确认结构；" : ""}识别月份：${analysis.months.join("、") || "未识别"}`
                : "请对照原表填写工作表、表头和列号"}
              description="DeepSeek 只认列；金额和每一行仍由系统本地重新读取。"
            />
            <Form layout="vertical">
              <Form.Item label="工作表名称" required>
                {analysis?.sheets?.length ? (
                  <Select
                    aria-label="工作表名称"
                    value={mapping.sheet}
                    options={analysis.sheets.map((sheet) => ({ value: sheet.name, label: `${sheet.name}（${sheet.row_count} 行）` }))}
                    onChange={(value) => setMapping({ ...mapping, sheet: value, header_row: 0 })}
                  />
                ) : (
                  <Input value={mapping.sheet} onChange={(event) => setMapping({ ...mapping, sheet: event.target.value })} />
                )}
              </Form.Item>
              <Form.Item label="表头在第几行" required>
                <InputNumber min={1} max={20} value={mapping.header_row + 1} onChange={(value) => setMapping({ ...mapping, header_row: Math.max((value ?? 1) - 1, 0) })} />
              </Form.Item>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 10 }}>
                {[["date", "日期列"], [amountField, role === "receipt" ? "已收金额列" : "费用金额列"], ...OPTIONAL_FIELDS].map(([field, label], index) => (
                  <Form.Item key={field} label={label} required={index < 2} style={{ marginBottom: 0 }}>
                    <Select
                      allowClear={index >= 2}
                      value={mapping.columns[field]}
                      placeholder={index < 2 ? "必填" : "可选"}
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
