export const feedbackSteps = [
  ["start", "进入页面 / 选择月份"], ["upload", "上传资料 / 识别文件"],
  ["receipts", "平台账单 / 线下收款"], ["cleaning", "保洁核对"],
  ["expenses", "费用 / 确认执行"], ["statements", "业主账单 / 下载"],
  ["recovery", "进度 / 刷新 / 失败重试"], ["other", "其他 / 不知道卡在哪一步"],
] as const;
export type FeedbackStep = typeof feedbackSteps[number][0];
export type FeedbackStatus = "received" | "processing" | "resolved";
export const feedbackStatusLabels: Record<FeedbackStatus, string> = {
  received: "已收到，待处理", processing: "处理中", resolved: "已处理，待你复试",
};
export interface MonthlyCloseFeedbackInput {
  request_id: string; billing_month: string; step: FeedbackStep;
  description: string; expected: string; execution_clicked: "yes" | "no" | "unknown";
  screenshot_base64?: string;
}
export interface MonthlyCloseFeedback {
  feedback_id: string; billing_month: string; step: FeedbackStep;
  description: string; expected: string; execution_clicked: "yes" | "no" | "unknown";
  screenshot_mime: string | null; status: FeedbackStatus; reply: string;
  revision: number; created_at: string; updated_at: string;
}
