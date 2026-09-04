"use client";

import { useRef } from "react";
import { useModalDialog } from "@/hooks/useModalDialog";
import type { PublishJob } from "@/features/content/types";
import { getPublishFailurePresentation, mapPublishStage, type PublishStageLabel } from "@/lib/miniapp-content";
import styles from "./content-center.module.css";

const technicalStages: PublishStageLabel[] = ["排队", "校验", "媒体发布", "指针切换", "线上验证", "成功", "失败", "已回滚"];
const businessStages = ["正在检查内容", "正在生成线上版本", "正在验证", "已上线"] as const;

type BusinessPublishStage = (typeof businessStages)[number];

function businessPublishStage(job: PublishJob | null): BusinessPublishStage {
  if (job?.status === "succeeded" || job?.status === "rolled_back") return "已上线";
  if (["verifying_candidate", "swapping_pointer", "observing_pointer", "finalizing"].includes(job?.progressStage || "")) {
    return "正在验证";
  }
  if (["promoting_media", "manifest_intent", "uploading_manifest"].includes(job?.progressStage || "")) {
    return "正在生成线上版本";
  }
  return "正在检查内容";
}

export function PublishDialog({
  open,
  job,
  onClose,
  onRetry,
  retryDisabled = false,
  trackingStatus = "success",
  trackingError,
  onRetryTracking,
  contentTitle = "业主频道",
  isAdmin = false,
}: {
  open: boolean;
  job: PublishJob | null;
  onClose: () => void;
  onRetry?: () => void;
  retryDisabled?: boolean;
  trackingStatus?: "idle" | "loading" | "success" | "error";
  trackingError?: string | null;
  onRetryTracking?: () => void;
  contentTitle?: string;
  isAdmin?: boolean;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  useModalDialog({ open, dialogRef, initialFocusRef: closeRef, onClose });
  if (!open) return null;

  const current = businessPublishStage(job);
  const technicalCurrent = job ? mapPublishStage(job) : "排队";
  const failed = job?.status === "failed";
  const isRollback = job?.operation === "rollback";
  const failure = getPublishFailurePresentation(job?.errorCode || job?.progressStage);
  const retryLabel = job?.operation === "rollback" && job.targetVersion
    ? `重新回滚 ${job.targetVersion}`
    : "重新发布";
  return (
    <div className={styles.overlay}>
      <aside ref={dialogRef} className={styles.contentDrawer} role="dialog" aria-modal="true" aria-label={isRollback ? "回滚发布版本" : `发布${contentTitle}`} tabIndex={-1}>
        <div className={styles.drawerHeader}>
          <div><h2>{isRollback ? "正在恢复历史版本" : "发布到小程序"}</h2><p>{contentTitle}</p></div>
          <button ref={closeRef} type="button" className={`touchTarget ${styles.touchTarget} ${styles.iconButton}`} onClick={onClose} aria-label="关闭发布进度">×</button>
        </div>
        <p className={styles.durableNote}>关闭页面不会取消服务器{isRollback ? "回滚" : "发布"}任务</p>
        {trackingStatus === "loading" ? (
          <div className={styles.dedicatedState} role="status">
            <h3>正在恢复发布任务状态</h3>
            <p>服务器任务仍会继续执行，请稍候。</p>
          </div>
        ) : null}
        {trackingStatus === "error" ? (
          <div className={styles.persistentError} role="alert">
            <h3>发布任务状态加载失败</h3>
            <p>{trackingError || "暂时无法取得服务器任务状态，任务本身不会被取消。"}</p>
            {onRetryTracking ? <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} onClick={onRetryTracking}>重试查询发布任务</button> : null}
          </div>
        ) : null}
        {trackingStatus !== "loading" && trackingStatus !== "error" && !failed ? (
          <>
        <p className={styles.currentStage} role="status">当前：{current}</p>
        <ol className={styles.stageList}>
          {businessStages.map((stage) => (
            <li key={stage} data-current={stage === current ? "true" : "false"}>
              <span aria-hidden="true">{stage === current ? "●" : "○"}</span>
              <span>{stage}</span>
            </li>
          ))}
        </ol>
        {job && isAdmin ? (
          <details className={styles.publishTechnicalDetails}>
            <summary style={{ minHeight: 44 }}>技术详情</summary>
            <div className={styles.publishTechnicalBody}>
              <p>当前：{technicalCurrent}</p>
              <p>任务编号：{job.jobId}</p>
              <p>内部阶段：{job.progressStage || "未提供"}</p>
              <p>处理进度：{job.progressPercent ?? 0}%</p>
              <ul>{technicalStages.map((stage) => <li key={stage}>{stage}</li>)}</ul>
            </div>
          </details>
        ) : null}
          </>
        ) : null}
        {trackingStatus !== "loading" && trackingStatus !== "error" && failed ? (
          <div className={styles.persistentError} role="alert">
            <h3>{failure.title}</h3>
            <p>{failure.operatorGuidance || (failure.canRetry && onRetry
              ? "可安全重试：将创建一个新的同类任务，本次失败任务不会被继续执行。"
              : "不可直接重试：请返回编辑检查内容后再提交。")}</p>
            {!failure.operatorGuidance ? <p>本地草稿仍然保留。</p> : null}
            {isAdmin ? <details className={styles.publishTechnicalDetails}>
              <summary style={{ minHeight: 44 }}>失败技术信息</summary>
              <p className={styles.jobMeta}>内部代码：{job.errorCode || "unknown"} · 阶段：{job.progressStage || "unknown"}</p>
            </details> : null}
            {failure.canRetry && onRetry ? <button type="button" disabled={retryDisabled} aria-busy={retryDisabled} className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} onClick={onRetry}>{retryLabel}</button> : <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} onClick={onClose}>{failure.closeLabel || "返回编辑检查"}</button>}
          </div>
        ) : null}
      </aside>
    </div>
  );
}
