"use client";
import styles from "./MonthlyCloseWorkspace.module.css";
import type { MonthlyCloseProjection } from "@/lib/monthly-close";

export function MonthOutcome({ projection, fileCount, onFiles, onFinish }: {
  projection: MonthlyCloseProjection; fileCount: number; onFiles: () => void; onFinish?: () => void;
}) {
  const review = projection.final_review;
  const confirmed = review.confirmed_settlement_count ?? 0;
  const paid = review.paid_settlement_count ?? 0;
  const complete = review.state === "verified";
  const ownersDone = review.settlement_count > 0 && confirmed === review.settlement_count;
  return <section className={styles.outcome} aria-label="本月当前结果">
    <h3>结算进度</h3>
    <dl>
      <dt>业主账单</dt><dd style={{ margin: 0 }}>{ownersDone ? `${confirmed} 份已确认，可以查看和下载` : review.settlement_count ? `${review.settlement_count} 份中 ${confirmed} 份已确认，其余需复核` : "尚未生成，先核对本月记录与费用"}</dd>
      <dt>付款登记</dt><dd style={{ margin: 0 }}>{review.settlement_count ? `已登记 ${paid} 份，未登记 ${Math.max(0, review.settlement_count - paid)} 份；未登记不代表实际未打款` : "生成并确认账单后，按实际打款登记"}</dd>
      <dt>公司经营账</dt><dd style={{ margin: 0 }}>{complete ? "已核验并关账" : "本工作流尚未关账；历史处理结果以已有确认记录为准"}</dd>
    </dl>
    <details><summary style={{ minHeight: 44, cursor: "pointer", display: "flex", alignItems: "center" }}>下一步怎么做</summary>
      <p style={{ margin: "8px 0" }}>{complete ? "本月已结束。需要调整时走重新打开月结流程。" : ownersDone ? "现在可以交付已确认的业主账单。公司收支资料由经办人补充，管理员核实后再关账。" : "下一步：先核对已收到的资料，处理缺项后再确认业主账单。"}</p>
      {onFinish && <button className="mcw-progress-button" onClick={onFinish}>{ownersDone ? "查看交付与剩余事项" : "查看结算进度"}</button>}
    </details>
    <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
      <button className="mcw-progress-button" onClick={onFiles}>查看全部资料（{fileCount} 份）</button>
    </div>
  </section>;
}
