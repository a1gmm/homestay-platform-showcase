"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useModalDialog } from "@/hooks/useModalDialog";
import { useUnsavedNavigationGuard } from "@/hooks/useUnsavedNavigationGuard";
import { SaveState, type OwnerSaveState } from "./SaveState";
import styles from "./content-center.module.css";

export type ContentEditorRole = "admin" | "operator";
export type ContentPreviewWidth = 375 | 390 | 430;

export interface ContentEditorAction {
  key: string;
  label: string;
  kind: "primary" | "secondary" | "danger";
  onClick(): void | Promise<unknown>;
  adminOnly?: boolean;
  disabled?: boolean;
  busy?: boolean;
  confirmation?: {
    title: string;
    impact: string;
    confirmLabel: string;
  };
}

export interface ContentEditorShellProps {
  title: string;
  explanation: string;
  statusLabel: string;
  statusExplanation: string;
  completion?: string;
  nextStep: string;
  role: ContentEditorRole;
  saveState: OwnerSaveState;
  hasUnsavedChanges: boolean;
  readyForPublish: boolean;
  operatorGuidance?: string;
  onSave(): Promise<unknown>;
  backHref?: string;
  onNavigate?(href: string): void;
  form: ReactNode;
  renderPreview(width: ContentPreviewWidth): ReactNode;
  actions: ContentEditorAction[];
  technicalDetails?: ReactNode;
}

const PREVIEW_SIZES: Array<{ width: ContentPreviewWidth; label: string }> = [
  { width: 375, label: "小屏手机" },
  { width: 390, label: "标准手机（推荐）" },
  { width: 430, label: "大屏手机" },
];

export function ContentEditorShell({
  title,
  explanation,
  statusLabel,
  statusExplanation,
  completion,
  nextStep,
  role,
  saveState,
  hasUnsavedChanges,
  readyForPublish,
  operatorGuidance = "内容已经准备好，请联系管理员发布。",
  onSave,
  backHref = "/content",
  onNavigate,
  form,
  renderPreview,
  actions,
  technicalDetails,
}: ContentEditorShellProps) {
  const [previewWidth, setPreviewWidth] = useState<ContentPreviewWidth>(390);
  const [confirmationAction, setConfirmationAction] = useState<ContentEditorAction | null>(null);
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const navigationDialogRef = useRef<HTMLDivElement>(null);
  const actionDialogRef = useRef<HTMLDivElement>(null);
  const navigationPrimaryRef = useRef<HTMLButtonElement>(null);
  const actionPrimaryRef = useRef<HTMLButtonElement>(null);

  const navigate = useCallback((href: string) => {
    if (onNavigate) onNavigate(href);
    else window.location.assign(href);
  }, [onNavigate]);
  const hasPendingWork = useCallback(() => hasUnsavedChanges, [hasUnsavedChanges]);
  const { pendingNavigation, navigationConfirming, navigationError, confirmNavigation, cancelNavigation, armHistoryGuard } = useUnsavedNavigationGuard({
    hasPendingWork,
    saveBeforeLeave: onSave,
    navigateTo: navigate,
  });
  useEffect(() => { if (hasUnsavedChanges) armHistoryGuard(); }, [armHistoryGuard, hasUnsavedChanges]);
  useModalDialog({ open: !!pendingNavigation, dialogRef: navigationDialogRef, initialFocusRef: navigationPrimaryRef, onClose: cancelNavigation, closeDisabled: navigationConfirming });
  useModalDialog({ open: !!confirmationAction && !pendingNavigation, dialogRef: actionDialogRef, initialFocusRef: actionPrimaryRef, onClose: () => setConfirmationAction(null), closeDisabled: actionPending });

  const runAction = async (action: ContentEditorAction) => {
    if (action.disabled || action.busy || actionPending || pendingNavigation) return;
    if (action.confirmation) {
      setActionError(null);
      setConfirmationAction(action);
      return;
    }
    setActionError(null);
    try {
      await action.onClick();
    } catch {
      setActionError("操作没有完成，请检查网络后重试。");
    }
  };

  const confirmAction = async () => {
    if (!confirmationAction || actionPending) return;
    setActionPending(true);
    setActionError(null);
    try {
      await confirmationAction.onClick();
      setConfirmationAction(null);
    } catch {
      setActionError("操作没有完成，请检查网络后重试。");
    } finally {
      setActionPending(false);
    }
  };

  const visibleActions = actions.filter((action) => !action.adminOnly || role === "admin");
  const showOperatorGuidance = role !== "admin" && readyForPublish && saveState === "saved" && actions.some((action) => action.adminOnly);

  return (
    <main className={styles.editorShell}>
      <header className={styles.editorHeader}>
        <Link className={styles.breadcrumb} href={backHref}>
          返回小程序内容管理
        </Link>
        <div className={styles.editorHeadingRow}>
          <div>
            <h1 className={styles.editorTitle}>{title}</h1>
            <p className={styles.editorExplanation}>{explanation}</p>
          </div>
          <SaveState state={saveState} />
        </div>
      </header>

      <section className={styles.editorStatus} aria-label="当前内容状态" role="status">
        <div>
          <strong>{statusLabel}</strong>
          <p>{statusExplanation}</p>
        </div>
        <div className={styles.editorStatusFacts}>
          {completion ? <span>{completion}</span> : null}
          <span>下一步：{nextStep}</span>
        </div>
      </section>

      <div className={styles.editorWorkspace}>
        <section className={styles.editorForm} aria-label="填写内容">
          <h2>填写内容</h2>
          {form}
        </section>

        <section className={styles.editorPreview} aria-label="手机预览">
          <div className={styles.previewHeading}>
            <h2>手机预览</h2>
            <p>这里只是查看不同手机上的显示效果，不会改变正式内容。</p>
          </div>
          <div className={styles.previewSizeControls} aria-label="选择预览手机大小">
            {PREVIEW_SIZES.map((size) => (
              <button
                key={size.width}
                type="button"
                className={styles.previewSizeButton}
                aria-pressed={previewWidth === size.width}
                onClick={() => setPreviewWidth(size.width)}
              >
                {size.label}
              </button>
            ))}
          </div>
          <p className={styles.previewWidthNote}>只改变右侧预览宽度，不会改变真实内容</p>
          <div className={styles.previewCanvas} data-preview-width={previewWidth}>{renderPreview(previewWidth)}</div>
        </section>
      </div>

      {role === "admin" && technicalDetails ? (
        <details className={styles.editorTechnicalDetails}>
          <summary style={{ minHeight: 44 }}>技术详情</summary>
          <div className={styles.editorTechnicalBody}>{technicalDetails}</div>
        </details>
      ) : null}

      <section className={styles.stickyActions} aria-label="主要操作">
        <div>
          {showOperatorGuidance ? <p className={styles.operatorPublishNote}>{operatorGuidance}</p> : null}
          {actionError ? <p className={styles.actionError} role="alert">{actionError}</p> : null}
        </div>
        <div className={styles.stickyActionButtons}>
          {visibleActions.map((action) => (
            <button
              key={action.key}
              type="button"
              className={action.kind === "primary"
                ? styles.editorPrimaryAction
                : action.kind === "danger"
                  ? styles.editorDangerAction
                  : styles.editorSecondaryAction}
              disabled={action.disabled || action.busy}
              aria-busy={action.busy || undefined}
              onClick={() => void runAction(action)}
            >
              {action.label}
            </button>
          ))}
        </div>
      </section>

      {pendingNavigation ? (
        <div className={styles.overlay}>
          <div ref={navigationDialogRef} className={styles.confirmationDialog} role="dialog" aria-modal="true" aria-label="离开内容编辑页面" tabIndex={-1}>
            <h2>当前修改尚未保存</h2>
            <p>先保存再离开，可以避免丢失刚刚填写的内容。</p>
            {navigationError ? <p className={styles.actionError} role="alert">{navigationError}</p> : null}
            <div className={styles.dialogActions}>
              <button type="button" className={styles.editorSecondaryAction} disabled={navigationConfirming} onClick={cancelNavigation}>继续编辑</button>
              <button ref={navigationPrimaryRef} type="button" className={styles.editorPrimaryAction} disabled={navigationConfirming} aria-busy={navigationConfirming || undefined} onClick={() => void confirmNavigation()}>
                {navigationConfirming ? "正在保存…" : "保存后离开"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {!pendingNavigation && confirmationAction?.confirmation ? (
        <div className={styles.overlay}>
          <div ref={actionDialogRef} className={styles.confirmationDialog} role="dialog" aria-modal="true" aria-label={confirmationAction.confirmation.title} tabIndex={-1}>
            <h2>{confirmationAction.confirmation.title}</h2>
            <p>{confirmationAction.confirmation.impact}</p>
            {actionError ? <p className={styles.actionError} role="alert">{actionError}</p> : null}
            <div className={styles.dialogActions}>
              <button type="button" className={styles.editorSecondaryAction} disabled={actionPending} onClick={() => setConfirmationAction(null)}>取消</button>
              <button ref={actionPrimaryRef} type="button" className={styles.editorDangerAction} disabled={actionPending} aria-busy={actionPending || undefined} onClick={() => void confirmAction()}>
                {confirmationAction.confirmation.confirmLabel}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </main>
  );
}
