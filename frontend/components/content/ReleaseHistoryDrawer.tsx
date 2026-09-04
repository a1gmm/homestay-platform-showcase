"use client";

import { useEffect, useRef, useState } from "react";
import type { ContentRelease } from "@/features/content/types";
import { useModalDialog } from "@/hooks/useModalDialog";
import styles from "./content-center.module.css";

export function ReleaseHistoryDrawer({
  open,
  releases,
  canRollback,
  onClose,
  onRollback,
  status = "success",
  onRetry,
  actionsDisabled = false,
}: {
  open: boolean;
  releases: ContentRelease[];
  canRollback: boolean;
  onClose: () => void;
  onRollback: (version: string) => Promise<void>;
  status?: "loading" | "success" | "error";
  onRetry?: () => void;
  actionsDisabled?: boolean;
}) {
  const [confirmVersion, setConfirmVersion] = useState<string | null>(null);
  const [isRollingBack, setIsRollingBack] = useState(false);
  const [rollbackError, setRollbackError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const activeDialogRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) {
      setConfirmVersion(null);
      setRollbackError(null);
    }
  }, [open]);
  useModalDialog({
    open,
    dialogRef: activeDialogRef,
    initialFocusRef: confirmVersion ? confirmRef : closeRef,
    onClose: confirmVersion ? () => setConfirmVersion(null) : onClose,
    closeDisabled: isRollingBack,
    instanceKey: confirmVersion || "history",
  });
  if (!open) return null;
  const rollbackEligible = (release: ContentRelease) => ["published", "superseded", "rolled_back"].includes(release.status);
  const releaseStatus = (statusValue: string) => statusValue === "published" ? "当前线上内容" : statusValue === "superseded" || statusValue === "rolled_back" ? "历史内容" : "未完成上线";
  const publishedAt = (value: string | null) => {
    if (!value) return "上线时间暂不可用";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "上线时间暂不可用" : `${date.toLocaleString("zh-CN")} 上线`;
  };
  return (
    <div className={styles.overlay}>
      {!confirmVersion ? <aside ref={(node) => { activeDialogRef.current = node; }} className={styles.contentDrawer} role="dialog" aria-modal="true" aria-label="发布历史" tabIndex={-1}>
        <div className={styles.drawerHeader}>
          <div>
            <h2>发布历史</h2>
            <p>{canRollback ? "查看以前上线过的内容，选择需要恢复的版本。" : "查看以前上线过的内容。恢复历史版本需要管理员操作。"}</p>
          </div>
          <button ref={closeRef} type="button" className={`touchTarget ${styles.touchTarget} ${styles.iconButton}`} onClick={onClose} aria-label="关闭发布历史">×</button>
        </div>
        {status === "loading" ? (
          <div className={styles.dedicatedState} role="status">
            <h3>正在加载发布历史</h3>
            <p>正在读取服务器版本记录。</p>
          </div>
        ) : status === "error" ? (
          <div className={styles.persistentError} role="alert">
            <h3>发布历史加载失败</h3>
            <p>暂时无法读取版本记录，请重试。</p>
            {onRetry ? <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} onClick={onRetry}>重试加载发布历史</button> : null}
          </div>
        ) : releases.length === 0 ? (
          <div className={styles.dedicatedState}>
            <h3>还没有发布历史</h3>
            <p>完成第一次发布后，版本与回滚入口会出现在这里。</p>
            <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} onClick={onClose}>返回编辑</button>
          </div>
        ) : (
          <ul className={styles.releaseList}>
            {releases.map((release) => (
              <li key={release.version}>
                <div>
                  <strong>{publishedAt(release.publishedAt)}</strong>
                  <span>{releaseStatus(release.status)}</span>
                  {canRollback ? (
                    <details className={styles.releaseTechnicalDetails}>
                      <summary style={{ minHeight: 44 }}>技术详情</summary>
                      <code>{release.version}</code>
                    </details>
                  ) : null}
                </div>
                {canRollback && rollbackEligible(release) ? (
                  <button
                    type="button"
                    aria-label={`回滚到 ${release.version}`}
                    disabled={actionsDisabled}
                    className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`}
                    onClick={() => { if (!actionsDisabled) { setRollbackError(null); setConfirmVersion(release.version); } }}
                  >
                    恢复这个版本
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </aside> : null}
      {confirmVersion ? (
        <div ref={(node) => { activeDialogRef.current = node; }} className={styles.confirmationDialog} role="dialog" aria-modal="true" aria-label="确认回滚" tabIndex={-1}>
          <h2>确认恢复历史版本</h2>
          <p>确认后，小程序用户会看到这个历史版本，当前线上内容会被替换。当前草稿不会删除，稍后仍可继续编辑。</p>
          <details className={styles.publishTechnicalDetails}>
            <summary style={{ minHeight: 44 }}>技术详情</summary>
            <p>目标版本：{confirmVersion}</p>
          </details>
          {rollbackError ? <p className={styles.persistentError} role="alert">{rollbackError}。未创建回滚任务，请重试。</p> : null}
          <div className={styles.dialogActions}>
            <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`} onClick={() => setConfirmVersion(null)}>取消</button>
            <button
              ref={confirmRef}
              type="button"
              aria-label={`确认回滚到 ${confirmVersion}`}
              disabled={isRollingBack}
              className={`touchTarget ${styles.touchTarget} ${styles.dangerButton}`}
              onClick={async () => {
                setIsRollingBack(true);
                setRollbackError(null);
                try {
                  await onRollback(confirmVersion);
                  setConfirmVersion(null);
                  onClose();
                } catch (error) {
                  setRollbackError(error instanceof Error ? error.message : "回滚请求失败");
                } finally {
                  setIsRollingBack(false);
                }
              }}
            >确认恢复这个版本</button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
