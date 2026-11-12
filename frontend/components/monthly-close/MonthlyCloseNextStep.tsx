import type { MonthlyCloseProjection } from "@/lib/monthly-close";
import styles from "./MonthlyCloseWorkspace.module.css";

export function MonthlyCloseNextStep({ projection, fileCount, title, description, label, disabled, onContinue }: {
  projection: MonthlyCloseProjection; fileCount: number; title: string; description: string;
  label?: string; disabled: boolean; onContinue: () => void;
}) {
  return <section className={styles.nextStep} aria-label="当前任务与下一步">
    <div className={styles.nextStepCopy}>
      <p className={styles.nextStepStatus}>{projection.recommended_action.kind === "reopen_cycle" ? "历史月份待重新核对" : projection.final_review.state === "verified" ? "本月已结束" : `已收到 ${fileCount} 份资料`}</p>
      <h2>{title}</h2>
      <p>{description}</p>
    </div>
    {label && <button type="button" className="mcw-primary-button" disabled={disabled} onClick={onContinue}>{label}</button>}
  </section>;
}
