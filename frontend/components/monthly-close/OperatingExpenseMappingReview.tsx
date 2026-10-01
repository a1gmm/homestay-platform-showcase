import { useState } from "react";
import { Alert, Button, Form, Input, InputNumber, Modal, Select, Space, message } from "antd";

import { monthlyCloseApi } from "@/lib/api";
import { extractErrorCode, extractErrorMessage } from "@/lib/api-errors";
import type {
  MonthlyCloseDocument,
  OperatingExpenseAnalysis,
  OperatingExpenseMapping,
} from "@/lib/monthly-close";
import { SourceProgressAlert, SourceProposalCard, type SourceProgressState } from "./SourceProposalCard";
import { SourceProposalHistory } from "./SourceProposalHistory";
import { RecognitionSummary } from "./RecognitionSummary";
import { useSourceProposalRecovery } from "./useSourceProposalRecovery";
import { sourceRequestId } from "./sourceRequestId";

const FIELD_LABELS: Array<[string, string, boolean]> = [
  ["date", "发生日期列", true],
  ["category", "费用类别列", true],
  ["amount", "金额列", true],
  ["description", "用途／说明列", true],
  ["room", "房间列", false],
  ["payer", "支付方列", false],
  ["notes", "备注列", false],
  ["reference", "外部单据号列", false],
];

const CATEGORY_OPTIONS = [
  ["cleaning", "保洁"], ["maintenance", "维修费"], ["supplies", "采购费"],
  ["platform_fee", "平台佣金"], ["tax", "税费"], ["other", "其他费用"],
  ["public_utilities", "公摊水费"], ["water", "水费"], ["broadband", "宽带费"],
  ["daily_supplies", "日耗"], ["laundry", "洗涤"], ["gas", "燃气费"],
  ["property_fee", "物业费"], ["electricity", "电费"], ["kitchen_cleaning", "厨房保洁"],
  ["property_guidance_fee", "物业引导费"], ["new_linen_prewash", "新布草过水费"],
].map(([value, label]) => ({ value, label }));

const PAYER_OPTIONS = [
  { value: "company", label: "公司承担" },
  { value: "owner", label: "业主承担" },
];

function setColumn(
  mapping: OperatingExpenseMapping,
  field: string,
  value: number | null,
): OperatingExpenseMapping {
  const columns = { ...mapping.columns };
  if (value === null) delete columns[field];
  else columns[field] = value;
  return { ...mapping, columns };
}

function money(value: string) {
  return Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
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

export function OperatingExpenseMappingReview({
  billingMonth,
  document,
  disabled,
  role = "admin",
  onFinished,
}: {
  billingMonth: string;
  document: MonthlyCloseDocument;
  disabled?: boolean;
  role?: string;
  onFinished: () => Promise<unknown>;
}) {
  const [analysis, setAnalysis] = useState<OperatingExpenseAnalysis | null>(null);
  const [mapping, setMapping] = useState<OperatingExpenseMapping | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [mappingModalReady, setMappingModalReady] = useState(false);
  const [analysisUnavailable, setAnalysisUnavailable] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [proposal, setProposal, proposalHistory, reloadProposalQueue, queueSourceState] = useSourceProposalRecovery(
    billingMonth,
    "operating_expense_import",
  );
  const [sourceState, setSourceState] = useState<SourceProgressState | null>(null);

  const analyze = async () => {
    setAnalyzing(true);
    setSourceState(null);
    setAnalysisUnavailable(false);
    setAdvancedOpen(false);
    try {
      const response = await monthlyCloseApi.analyzeOperatingExpense(
        billingMonth,
        document.document_id,
      );
      setAnalysis(response.data);
      setMapping(response.data.mapping);
    } catch (error) {
      message.error(extractErrorMessage(error, "表格结构识别失败，可重试或人工确认"));
      setAnalysisUnavailable(true);
      setMapping({
        sheet: "Sheet1",
        header_row: 0,
        columns: { date: 0, category: 1, amount: 2, description: 3 },
      });
    } finally {
      setAnalyzing(false);
    }
  };

  const confirm = async () => {
    if (!mapping || !mappingModalReady) return;
    setConfirming(true);
    try {
      const checked = await monthlyCloseApi.analyzeOperatingExpense(
        billingMonth,
        document.document_id,
        mapping,
      );
      await monthlyCloseApi.confirmOperatingExpense(
        billingMonth,
        document.document_id,
        checked.data.mapping,
      );
      try {
        const proposalResponse = await monthlyCloseApi.createOperatingExpenseProposal(
          billingMonth,
          sourceRequestId(
            "operating-expense",
            billingMonth,
            document.document_id,
            checked.data.mapping,
          ),
        );
        setProposal(proposalResponse.data);
        setSourceState(null);
        message.success("已保存确认结构，费用方案等待确认");
      } catch (error) {
        if (extractErrorCode(error) !== "operating_expense_no_safe_commands") throw error;
        setSourceState("需要确认");
        message.warning("已保存确认结构，当前无可安全执行的费用变更");
      }
      setMapping(null);
      setAnalysis(null);
      setAnalysisUnavailable(false);
      setAdvancedOpen(false);
      await onFinished();
    } catch (error) {
      message.error(extractErrorMessage(error, "字段映射无法解析，请检查工作表和列号"));
    } finally {
      setConfirming(false);
    }
  };

  const requiredComplete = mapping
    && mapping.sheet.trim()
    && ["date", "category", "amount", "description"]
      .every((field) => mapping.columns[field] !== undefined)
    && (analysis?.unmapped_categories ?? []).every((raw) => mapping.category_values?.[raw])
    && (analysis?.unmapped_payers ?? []).every((raw) => mapping.payer_values?.[raw]);
  const selectedSheet = analysis?.sheets?.find((sheet) => sheet.name === mapping?.sheet);
  const headerPreview = selectedSheet?.rows[mapping?.header_row ?? 0] ?? [];
  const columnOptions = Array.from(
    { length: Math.min(selectedSheet?.column_count ?? Math.max(headerPreview.length, 10), 100) },
    (_, index) => ({
      value: index,
      label: `${columnName(index)}${headerPreview[index] && headerPreview[index] !== "EMPTY" ? ` · ${headerPreview[index]}` : ""}`,
    }),
  );

  return (
    <>
      <SourceProposalHistory items={proposalHistory} />
      {proposal && (
        <SourceProposalCard
          billingMonth={billingMonth}
          proposal={proposal}
          role={role}
          onProposal={setProposal}
          onFinished={async () => {
            await reloadProposalQueue();
            await onFinished();
          }}
        />
      )}
      {!proposal && sourceState && <SourceProgressAlert state={sourceState} />}
      <Button disabled={disabled || queueSourceState?.state === "completed"} loading={analyzing} onClick={() => void analyze()}>
        {queueSourceState?.state === "completed" ? "当前来源已对完" : proposal ? "继续处理方案" : analyzing ? "正在识别" : "查看识别结果"}
      </Button>
      <Modal
        title="确认其他运营支出"
        open={mapping !== null}
        okText="识别正确，生成核对方案"
        cancelText="稍后处理"
        confirmLoading={confirming}
        okButtonProps={{ disabled: !requiredComplete || !mappingModalReady || (analysisUnavailable && (!advancedOpen || role !== "admin")) }}
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
        {mapping && (
          <div style={{ display: "grid", gap: 16 }}>
            <RecognitionSummary
              documentName={document.filename}
              sourceLabel="其他运营支出"
              facts={analysis ? [
                { label: "可核对记录", value: `${analysis.valid_row_count} 条` },
                { label: "金额合计", value: `¥${money(analysis.total_amount)}` },
                { label: "需要修正", value: `${analysis.invalid_row_count} 行` },
              ] : []}
              warning={analysisUnavailable
                ? "系统没能自动读懂这份表格。文件已经保存，请让管理员确认一次读取方式。"
                : analysis?.suggested_by === "administrator"
                  ? "系统没有把握判断表格结构，请先核对记录数和金额。"
                  : undefined}
              note={analysis?.suggested_by === "remembered"
                ? "已沿用上月确认过的读取方式。确认后系统会重新校验日期、类别、金额和合计。"
                : "确认后系统会重新校验日期、类别、金额和合计；确认前不会写入费用。"}
              isAdmin={role === "admin"}
              onAdvancedChange={setAdvancedOpen}
              advanced={<Form layout="vertical">
                <Form.Item label="工作表名称" htmlFor="expense-mapping-sheet" required>
                  {analysis?.sheets?.length ? (
                    <Select
                      aria-label="工作表名称"
                      value={mapping.sheet}
                      options={analysis.sheets.map((sheet) => ({ value: sheet.name, label: `${sheet.name}（${sheet.row_count} 行）` }))}
                      onChange={(value) => setMapping({ ...mapping, sheet: value, header_row: 0 })}
                    />
                  ) : (
                    <Input
                      id="expense-mapping-sheet"
                      value={mapping.sheet}
                      onChange={(event) => setMapping({ ...mapping, sheet: event.target.value })}
                    />
                  )}
                </Form.Item>
                <Form.Item label="表头在第几行" htmlFor="expense-mapping-header-row" required>
                  <InputNumber
                    id="expense-mapping-header-row"
                    min={1}
                    max={30}
                    value={mapping.header_row + 1}
                    onChange={(value) => setMapping({ ...mapping, header_row: Math.max((value ?? 1) - 1, 0) })}
                  />
                </Form.Item>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
                  {FIELD_LABELS.map(([field, label, required]) => (
                    <Form.Item
                      key={field}
                      label={label}
                      htmlFor={`expense-mapping-${field}`}
                      required={required}
                      style={{ marginBottom: 0 }}
                      validateStatus={(analysis?.field_confidence[field] ?? 1) < 0.8 ? "warning" : undefined}
                      help={(analysis?.field_confidence[field] ?? 1) < 0.8 ? (analysis?.reasons[field] || "智能识别把握较低，请对照原表确认") : undefined}
                    >
                      <Select
                        id={`expense-mapping-${field}`}
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
            {analysis && analysis.failures.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message="以下行不会入账"
                description={(
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {analysis.failures.map((failure) => (
                      <li key={`${failure.row}-${failure.errors}`}>{failure.errors}</li>
                    ))}
                  </ul>
                )}
              />
            )}
            {analysis && ((analysis.unmapped_categories?.length ?? 0) > 0 || (analysis.unmapped_payers?.length ?? 0) > 0) && (
              <section aria-label="确认原表自定义值" style={{ border: "0.5px solid var(--linen)", borderRadius: 8, padding: 12 }}>
                <div style={{ fontWeight: 500, marginBottom: 10 }}>只需确认一次这些原表写法，下个月相同格式会自动沿用</div>
                <Space direction="vertical" size={10} style={{ width: "100%" }}>
                  {analysis.unmapped_categories?.map((raw) => (
                    <div key={`category-${raw}`} style={{ display: "grid", gridTemplateColumns: "minmax(120px, 1fr) minmax(180px, 1fr)", gap: 10, alignItems: "center" }}>
                      <span>费用类别“{raw}”</span>
                      <Select
                        aria-label={`费用类别${raw}`}
                        placeholder="选择系统费用类别"
                        options={CATEGORY_OPTIONS}
                        value={mapping.category_values?.[raw]}
                        onChange={(value) => setMapping({
                          ...mapping,
                          category_values: { ...(mapping.category_values ?? {}), [raw]: value },
                        })}
                      />
                    </div>
                  ))}
                  {analysis.unmapped_payers?.map((raw) => (
                    <div key={`payer-${raw}`} style={{ display: "grid", gridTemplateColumns: "minmax(120px, 1fr) minmax(180px, 1fr)", gap: 10, alignItems: "center" }}>
                      <span>支付方“{raw}”</span>
                      <Select
                        aria-label={`支付方${raw}`}
                        placeholder="选择谁承担"
                        options={PAYER_OPTIONS}
                        value={mapping.payer_values?.[raw]}
                        onChange={(value) => setMapping({
                          ...mapping,
                          payer_values: { ...(mapping.payer_values ?? {}), [raw]: value },
                        })}
                      />
                    </div>
                  ))}
                </Space>
              </section>
            )}
            {analysis?.replacement_context
              && analysis.replacement_context.current_unreferenced_rows.length > 0 && (
              <section
                aria-label="确认替代原件行对应关系"
                style={{ border: "0.5px solid var(--linen)", borderRadius: 8, padding: 12 }}
              >
                <div style={{ fontWeight: 500, marginBottom: 10 }}>
                  如果行在更正版中移动，请明确选择对应的前版行
                </div>
                <Space direction="vertical" size={10} style={{ width: "100%" }}>
                  {analysis.replacement_context.current_unreferenced_rows.map((rowNumber) => {
                    const selected = mapping.predecessor_rows?.[String(rowNumber)];
                    return (
                      <Select
                        key={rowNumber}
                        aria-label={`当前第 ${rowNumber} 行对应前版行`}
                        allowClear
                        placeholder="未移动可留空；移动时必须选择"
                        value={selected ? JSON.stringify(selected) : undefined}
                        options={analysis.replacement_context?.predecessor_rows.map((item) => ({
                          value: JSON.stringify({
                            document_id: item.document_id,
                            row_number: item.row_number,
                          }),
                          label: `原件 ${item.document_id} · ${item.source_sheet}第 ${item.row_number} 行`,
                        }))}
                        onChange={(value) => setMapping({
                          ...mapping,
                          predecessor_rows: {
                            ...(mapping.predecessor_rows ?? {}),
                            [String(rowNumber)]: JSON.parse(value) as {
                              document_id: string;
                              row_number: number;
                            },
                          },
                        })}
                        onClear={() => {
                          const predecessorRows = { ...(mapping.predecessor_rows ?? {}) };
                          delete predecessorRows[String(rowNumber)];
                          setMapping({ ...mapping, predecessor_rows: predecessorRows });
                        }}
                        style={{ width: "100%" }}
                      />
                    );
                  })}
                </Space>
              </section>
            )}
          </div>
        )}
      </Modal>
    </>
  );
}
