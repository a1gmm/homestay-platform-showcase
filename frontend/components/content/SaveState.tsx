import { CheckCircleOutlined, CloseCircleOutlined, EditOutlined, ExclamationCircleOutlined, LoadingOutlined } from "@ant-design/icons";
import styles from "./content-center.module.css";

export type OwnerSaveState = "unsaved" | "saved" | "saving" | "failed" | "conflict";

const stateContent: Record<OwnerSaveState, { label: string; icon: React.ReactNode }> = {
  unsaved: { label: "有未保存修改", icon: <EditOutlined /> },
  saved: { label: "已保存", icon: <CheckCircleOutlined /> },
  saving: { label: "正在保存", icon: <LoadingOutlined /> },
  failed: { label: "保存失败", icon: <CloseCircleOutlined /> },
  conflict: { label: "内容冲突", icon: <ExclamationCircleOutlined /> },
};

export function SaveState({
  state,
  onRetry,
  onCompare,
}: {
  state: OwnerSaveState;
  onRetry?: () => void;
  onCompare?: () => void;
}) {
  const content = stateContent[state];
  return (
    <div className={`${styles.saveState} ${styles[`saveState_${state}`]}`} style={state === "unsaved" ? { color: "#71523d" } : undefined} role="status" aria-label="保存状态" aria-live="polite">
      <span className={styles.saveStateIcon} aria-hidden="true">{content.icon}</span>
      <span>{content.label}</span>
      {state === "failed" && onRetry ? (
        <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.inlineAction}`} onClick={onRetry}>重试保存</button>
      ) : null}
      {state === "conflict" && onCompare ? (
        <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.inlineAction}`} onClick={onCompare}>查看服务器与本地差异</button>
      ) : null}
    </div>
  );
}
