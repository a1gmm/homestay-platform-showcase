"use client";

import { useEffect, useRef, useState } from "react";
import { monthlyCloseApi } from "@/lib/api";
import { monthlyCloseTaskArtifactPath, type MonthlyCloseTask, type MonthlyCloseTaskCommand } from "@/lib/monthly-close-task";
import { downloadBlob } from "@/lib/utils";
import styles from "./MonthlyCloseTaskProgress.module.css";

const statusLabels = {
  queued: "已接下本月对账", running: "正在核对", waiting_user: "需要你补充事实",
  waiting_approval: "等待你确认方案", paused: "已暂停", succeeded: "任务已完成", failed: "任务暂未完成",
};
const stepLabels: Record<string, string> = { pending: "待处理", queued: "待处理", running: "核对中", completed: "已完成", succeeded: "已完成", blocked: "待补充", skipped: "不适用", waiting_user: "待补充", waiting_approval: "待确认", failed: "需重试" };
type Action = MonthlyCloseTask["actions"][number];
export interface MonthlyCloseTaskProgressProps {
  month: string;
  ownerSettlementsConfirmed?: boolean;
  task: MonthlyCloseTask | null;
  loading: boolean;
  pending: boolean;
  busy: boolean;
  error: string | null;
  onCommand: (input: Omit<MonthlyCloseTaskCommand, "expected_revision">) => Promise<unknown>;
  onAction: (action: Action) => Promise<unknown>;
  onRefresh: () => Promise<unknown>;
  onAnswer: () => void;
  questionAction?: (question: MonthlyCloseTask["questions"][number]) => { label: string; run: () => void } | null;
}

export function MonthlyCloseTaskProgress({ month, ownerSettlementsConfirmed = false, task, loading, pending, busy, error, onCommand, onAction, onRefresh, onAnswer, questionAction }: MonthlyCloseTaskProgressProps) {
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const downloadController = useRef<AbortController | null>(null);
  useEffect(() => () => downloadController.current?.abort(), []);
  const disabled = pending || busy;
  const currentAction = task?.actions[0];
  const waiting = task?.status === "waiting_user" || task?.status === "waiting_approval";
  const canResume = task?.status === "paused" || task?.status === "failed";
  const active = task?.status === "running" || task?.status === "queued";
  const groupedQuestions = new Map<string, MonthlyCloseTask["questions"]>();
  for (const question of task?.questions ?? []) {
    const group = groupedQuestions.get(question.subject) ?? [];
    group.push(question);
    groupedQuestions.set(question.subject, group);
  }
  const download = async (artifact: MonthlyCloseTask["artifacts"][number]) => {
    const path = monthlyCloseTaskArtifactPath(artifact.url, month);
    if (!path || downloadController.current) return;
    const controller = new AbortController();
    downloadController.current = controller;
    setDownloading(true);
    setDownloadError(null);
    try {
      const response = await monthlyCloseApi.downloadTaskArtifact(path, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = url;
      const disposition = String(response.headers?.["content-disposition"] ?? "");
      const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
      const plainName = disposition.match(/filename="([^"]+)"/i)?.[1];
      let filename = plainName || artifact.label;
      if (encodedName) { try { filename = decodeURIComponent(encodedName); } catch { /* Fall back to the visible label. */ } }
      link.download = filename.replace(/[\\/:*?"<>|]/g, "_");
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
    } catch {
      if (!controller.signal.aborted) setDownloadError("文件暂时没有下载成功，请重试。");
    } finally {
      downloadController.current = null;
      if (!controller.signal.aborted) setDownloading(false);
    }
  };
  return <section className={styles.root} aria-label="本月对账任务">
    <div className={styles.row}>
      <div className={styles.copy} role="status" aria-live="polite">
        <div>{loading ? "正在读取本月任务" : task ? statusLabels[task.status] : "把本月对账交给助理"}</div>
        <p>{task?.status === "paused" ? (ownerSettlementsConfirmed ? "暂停的是自动核对任务，已确认的业主账单保留。继续后会检查公司月结的剩余事项。" : "自动核对已暂停；已保存的记录保留。继续后会重新检查当前待办。") : task?.summary || (task ? "任务会保留在本月，回来后可继续。" : "上传原件后，我会逐项核对，只在缺少事实或需要确认时找你。")}</p>
        {active && task?.activity && <p>{task.activity}</p>}
      </div>
      {!loading && !error && !task && <button className={styles.primary} disabled={disabled} onClick={() => { void onCommand({ action: "start", goal: "帮我完成本月对账" }); }}>帮我完成本月对账</button>}
      {canResume && <button className={styles.primary} disabled={disabled} onClick={() => { void onCommand({ action: "resume" }); }}>{task?.status === "failed" ? "重试并继续" : (ownerSettlementsConfirmed ? "继续检查公司月结" : "继续对账")}</button>}
      {waiting && currentAction && <button className={styles.primary} disabled={disabled} onClick={() => { void onAction(currentAction); }}>{currentAction.label}</button>}
      {waiting && !currentAction && <button className={styles.primary} disabled={disabled} onClick={onAnswer}>{task?.status === "waiting_approval" ? "在对话中查看方案" : "补充事实"}</button>}
    </div>
    {error && <div className={styles.error} role="alert">{error}<button disabled={pending} onClick={() => { void onRefresh(); }}>重新读取任务</button></div>}
    {task && <details className={styles.details}>
      <summary>查看任务详情{task.questions.length > 0 ? ` · ${task.questions.length} 项待补充` : ""}</summary>
      <p>{task.goal}</p>
      {task.steps.length > 0 && <ol className={styles.steps}>{task.steps.map((step) => <li key={step.key}><span>{step.label} · {stepLabels[step.status] ?? "待核对"}</span>{step.detail && <p>{step.detail}</p>}</li>)}</ol>}
      {Array.from(groupedQuestions).map(([subject, questions]) => <section key={subject} aria-label={subject || "待补充事实"} className={styles.questions}>
        <h3>{subject || "待补充事实"}</h3><ul>{questions.map((question) => <li key={question.key}>{question.message}{questionAction?.(question) && <div><button disabled={disabled} onClick={() => questionAction(question)?.run()}>{questionAction(question)?.label}</button></div>}</li>)}</ul>
      </section>)}
      {task.questions.length > 0 && <button onClick={() => downloadBlob(new Blob([`${month} 对账待核实清单\n\n`, ...Array.from(groupedQuestions).map(([subject, questions]) => `${subject || "待补充事实"}\n${questions.map((question, index) => `${index + 1}. ${question.message}`).join("\n")}\n\n`)], { type: "text/plain;charset=utf-8" }), `${month}-对账待核实清单.txt`)}>下载待核实清单</button>}
      {waiting && task.actions.slice(1).map((action) => <button key={action.key} disabled={disabled} onClick={() => { void onAction(action); }}>{action.label}</button>)}
      {(active || waiting) && <button disabled={disabled} onClick={() => { void onCommand({ action: "pause" }); }}>暂停任务</button>}
    </details>}
    {task?.artifacts.length ? <div className={styles.artifacts} aria-label="对账交付文件">{task.artifacts.map((artifact) => {
      const valid = monthlyCloseTaskArtifactPath(artifact.url, month);
      return <button key={artifact.url} disabled={!valid || downloading} onClick={() => { void download(artifact); }}>{valid ? `下载${artifact.label}` : "此文件链接已不可用，请刷新任务"}</button>;
    })}</div> : null}
    {downloadError && <div className={styles.error} role="alert">{downloadError}</div>}
  </section>;
}
