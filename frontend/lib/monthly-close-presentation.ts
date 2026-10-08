import type {
  MonthlyCloseProjectedDocumentSummary,
  MonthlyCloseProjectionSource,
  MonthlyCloseWorkspaceSourceState,
} from "./monthly-close";

export type MonthlyCloseBusinessStage =
  | "uploading"
  | "uploaded"
  | "recognizing"
  | "needs_confirmation"
  | "reconciling"
  | "completed"
  | "problem";

export interface MonthlyCloseDocumentProgress {
  stage: MonthlyCloseBusinessStage;
  label: string;
  detail: string;
}

export interface MonthlyCloseProgressSummary {
  uploadedSourceCount: number;
  documentCount: number;
  pendingSourceCount: number;
  completedSourceCount: number;
  missingSourceCount: number;
  notApplicableSourceCount: number;
  text: string;
}

const COMPLETED: MonthlyCloseDocumentProgress = {
  stage: "completed",
  label: "已完成",
  detail: "这类资料已经核对完成。",
};

export function presentDocumentProgress(
  document: MonthlyCloseProjectedDocumentSummary,
  sourceState: MonthlyCloseWorkspaceSourceState,
): MonthlyCloseDocumentProgress {
  if (document.storage_state === "receiving") {
    return {
      stage: "uploading",
      label: "正在上传",
      detail: "文件还在传到系统，请保持当前页面打开。",
    };
  }
  if (["rejected", "quarantined"].includes(document.storage_state)) {
    return {
      stage: "problem",
      label: "文件未能保存",
      detail: "请检查文件格式后重新上传；如果仍然失败，请联系管理员。",
    };
  }
  if (sourceState === "completed") return COMPLETED;
  if (document.work_record_count) return { stage: "needs_confirmation", label: `已补齐 ${document.work_record_count} 条历史打扫记录`, detail: "补齐结果已保存。打开核对界面可查看已补齐记录和剩余差异。" };
  if (document.analysis_state === "work_log_ready") return {
    stage: "needs_confirmation", label: "已识别打扫记录",
    detail: document.analysis_message || "记录核对与费用核对分步进行；先核对打扫记录，再核对续住费用。",
  };
  if (sourceState === "blocked") {
    return {
      stage: "problem",
      label: "核对发现问题",
      detail: "请打开详情查看原因和需要处理的内容。",
    };
  }
  if (document.classification_state === "pending") {
    return {
      stage: "uploaded",
      label: "已上传，等待识别",
      detail: "文件已经保存，系统还在判断它属于哪类月结资料。",
    };
  }
  if (document.classification_state === "needs_confirmation") {
    return {
      stage: "needs_confirmation",
      label: "待你确认资料类型",
      detail: "系统不确定这是什么资料，请确认后继续。",
    };
  }
  if (document.classification_state === "failed") {
    return {
      stage: "problem",
      label: "资料类型未识别",
      detail: "文件已经保存。请重新识别；仍然失败时，让管理员选择资料类型。",
    };
  }
  if (["not_started", "queued", "running"].includes(document.analysis_state)) {
    return {
      stage: "recognizing",
      label: "正在识别",
      detail: document.analysis_state === "queued"
        ? "文件已经保存，正在等待系统读取。你可以留在本页查看进度。"
        : "系统正在读取表格内容，完成后会请你确认。",
    };
  }
  if (document.analysis_state === "needs_mapping") {
    return {
      stage: "needs_confirmation",
      label: "待你确认识别结果",
      detail: "文件已保存，请打开识别结果，查看资料内容和核对方式。",
    };
  }
  if (document.analysis_state === "failed") {
    return {
      stage: "problem",
      label: "识别未完成",
      detail: "文件已经保存，但还没读出可核对的记录。先点“重新识别”；仍未成功时，点“查看原表并确认列”，核对日期、房号等列的位置。",
    };
  }
  if (sourceState === "processing") {
    return {
      stage: "reconciling",
      label: "正在核对",
      detail: "识别结果已确认，系统正在与订单和已有记录核对。",
    };
  }
  return {
    stage: "needs_confirmation",
    label: "待你处理核对结果",
    detail: "表格已经读完，请查看核对结果和下一步。",
  };
}

export function presentSourceProgress(source: MonthlyCloseProjectionSource): string {
  if (source.state === "missing") return "还未上传";
  if (source.state === "not_applicable") return "本月不需要";
  if (source.state === "completed") return "已完成";
  if (source.state === "blocked") return "核对发现问题";
  const latestDocument = source.documents.at(-1);
  if (!latestDocument) return source.state === "processing" ? "处理中" : "待你处理";
  return presentDocumentProgress(latestDocument, source.state).label;
}

export function summarizeMonthlyCloseProgress(
  sources: MonthlyCloseProjectionSource[],
): MonthlyCloseProgressSummary {
  const uploadedSources = sources.filter((source) => source.documents.length > 0);
  const pendingSourceCount = sources.filter((source) => ["processing", "needs_action", "blocked"].includes(source.state)).length;
  const completedSourceCount = sources.filter((source) => source.state === "completed").length;
  const missingSourceCount = sources.filter((source) => source.state === "missing").length;
  const notApplicableSourceCount = sources.filter((source) => source.state === "not_applicable").length;
  const uploadedSourceCount = uploadedSources.length;
  return {
    uploadedSourceCount,
    documentCount: uploadedSources.reduce((count, source) => count + source.documents.length, 0),
    pendingSourceCount,
    completedSourceCount,
    missingSourceCount,
    notApplicableSourceCount,
    text: `已完成 ${completedSourceCount} 类 · 待处理 ${pendingSourceCount} 类 · 缺资料 ${missingSourceCount} 类${notApplicableSourceCount ? ` · 本月不需要 ${notApplicableSourceCount} 类` : ""}`,
  };
}
