import { useState } from "react";
import { Button, Form, Input, InputNumber, Modal, Select, message } from "antd";

import { monthlyCloseApi } from "@/lib/api";
import { extractErrorCode, extractErrorMessage } from "@/lib/api-errors";
import type { MonthlyCloseDocument, UtilityStatementAnalysis, UtilityStatementMapping } from "@/lib/monthly-close";
import { SourceProgressAlert, SourceProposalCard, type SourceProgressState } from "./SourceProposalCard";
import { SourceProposalHistory } from "./SourceProposalHistory";
import { RecognitionSummary } from "./RecognitionSummary";
import { useSourceProposalRecovery } from "./useSourceProposalRecovery";
import { sourceRequestId } from "./sourceRequestId";

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
  role: userRole = "admin",
  onFinished,
}: {
  billingMonth: string;
  document: MonthlyCloseDocument;
  sourceType: string;
  disabled?: boolean;
  role?: string;
  onFinished: () => Promise<unknown>;
}) {
  const role = sourceType === "utility_receipt" ? "receipt" : "expense";
  const amountField = role === "receipt" ? "receipt_amount" : "expense_amount";
  const [analysis, setAnalysis] = useState<UtilityStatementAnalysis | null>(null);
  const [mapping, setMapping] = useState<UtilityStatementMapping | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [analysisUnavailable, setAnalysisUnavailable] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [proposal, setProposal, proposalHistory, reloadProposalQueue, queueSourceState] = useSourceProposalRecovery(
    billingMonth,
    "utility_reconciliation",
  );
  const [sourceState, setSourceState] = useState<SourceProgressState | null>(null);
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
    setSourceState(null);
    setAnalysisUnavailable(false);
    setAdvancedOpen(false);
    try {
      const response = await monthlyCloseApi.analyzeUtilityStatement(
        billingMonth,
        document.document_id,
      );
      setAnalysis(response.data);
      setMapping(response.data.mapping);
    } catch (error) {
      message.error(extractErrorMessage(error, "自动识别失败，请人工确认列号"));
      setAnalysisUnavailable(true);
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
      if (role === "expense") {
        setProposal(null);
        setSourceState("已对完");
        message.success("水电支出格式已确认，本份资料已完成识别");
      } else {
        try {
          const proposalResponse = await monthlyCloseApi.createUtilityProposal(
            billingMonth,
            sourceRequestId("utility", billingMonth, document.document_id, checked.data.mapping),
          );
          setProposal(proposalResponse.data);
          setSourceState(null);
          message.success("已保存格式，方案等待确认");
        } catch (error) {
          if (extractErrorCode(error) !== "utility_no_safe_commands") throw error;
          setSourceState("需要确认");
          message.success("已保存格式；另一份资料就绪后可继续核对");
        }
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

  return (
    <>
      <SourceProposalHistory items={proposalHistory} />
      {proposal && (
        <SourceProposalCard
          billingMonth={billingMonth}
          proposal={proposal}
          role={userRole}
          onProposal={setProposal}
          onFinished={async () => {
            await reloadProposalQueue();
            await onFinished();
          }}
        />
      )}
      {!proposal && sourceState && <SourceProgressAlert state={sourceState} />}
      <Button size="small" disabled={disabled || queueSourceState?.state === "completed"} loading={analyzing} onClick={() => void analyze()}>
        {queueSourceState?.state === "completed" ? "当前来源已对完" : proposal ? "继续处理方案" : analyzing ? "正在识别" : "查看识别结果"}
      </Button>
      <Modal
        title={role === "receipt" ? "确认历史水电资料" : "确认水电支出"}
        open={mapping !== null}
        okText={role === "receipt" ? "识别正确，继续核对" : "识别正确，开始核对"}
        cancelText="稍后处理"
        confirmLoading={confirming}
        okButtonProps={{
          disabled: (analysisUnavailable && (!advancedOpen || userRole !== "admin"))
            || !mapping?.sheet.trim()
            || mapping.columns.date === undefined
            || mapping.columns[amountField] === undefined,
        }}
        onOk={() => void confirm()}
        onCancel={() => {
          setMapping(null);
          setAnalysis(null);
          setAnalysisUnavailable(false);
          setAdvancedOpen(false);
        }}
      >
        {mapping && (
          <RecognitionSummary
            documentName={document.filename}
            sourceLabel={role === "receipt" ? "历史水电资料" : "水电支出"}
            facts={analysis ? [
              { label: "资料月份", value: analysis.months.join("、") || "未识别" },
              { label: "识别到", value: `${analysis.record_count} 条` },
              { label: "金额合计", value: `¥${analysis.total_amount}` },
            ] : []}
            warning={analysisUnavailable
              ? "系统没能自动读懂这份表格。文件已经保存，请让管理员确认一次读取方式。"
              : analysis?.suggested_by === "administrator"
                ? "系统没有把握判断表格结构，请先核对资料月份。"
                : undefined}
            note={analysis?.suggested_by === "remembered"
              ? "已沿用上月确认过的读取方式。确认后系统会重新读取每一行。"
              : "确认后系统会重新读取每一行；智能识别不会直接修改账目。"}
            isAdmin={userRole === "admin"}
            onAdvancedChange={setAdvancedOpen}
            advanced={<Form layout="vertical">
              <Form.Item label="工作表名称" htmlFor="utility-mapping-sheet" required>
                {analysis?.sheets?.length ? (
                  <Select
                    aria-label="工作表名称"
                    value={mapping.sheet}
                    options={analysis.sheets.map((sheet) => ({ value: sheet.name, label: `${sheet.name}（${sheet.row_count} 行）` }))}
                    onChange={(value) => setMapping({ ...mapping, sheet: value, header_row: 0 })}
                  />
                ) : (
                  <Input id="utility-mapping-sheet" value={mapping.sheet} onChange={(event) => setMapping({ ...mapping, sheet: event.target.value })} />
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
            </Form>}
          />
        )}
      </Modal>
    </>
  );
}
