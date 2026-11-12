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
      <dt>业主账单</dt><dd style={{ margin: 0 }}>{ownersDone ? `${confirmed} 份已确认，可以查看和下载` : review.settlement_count ? `已生成 ${review.settlement_count} 份，${Math.max(0, review.settlement_count - confirmed)} 份待复核确认` : "尚未生成，先核对本月记录与费用"}</dd>
      <dt>付款登记</dt><dd style={{ margin: 0 }}>{review.settlement_count ? `已登记 ${paid} 份，未登记 ${Math.max(0, review.settlement_count - paid)} 份；未登记不代表实际未打款` : "生成并确认账单后，按实际打款登记"}</dd>
      <dt>业主对账</dt><dd style={{ margin: 0 }}>{complete ? "已完成" : "待完成；已确认的账单可查看和下载"}</dd>
    </dl>
    <details><summary style={{ minHeight: 44, cursor: "pointer", display: "flex", alignItems: "center" }}>下一步怎么做</summary>
      <p style={{ margin: "8px 0" }}>{complete ? "本月已结束。需要调整时走重新打开月结流程。" : ownersDone ? "现在可以交付已确认的业主账单，并完成本月业主对账。" : review.settlement_count ? "打开已生成的账单，核对收入、扣费和应付金额。新增或更正资料后，先检查变化，再决定是否重算。" : "先上传平台账单、保洁表和水电表，核实差异及费用后，再生成业主账单。"}</p>
    </details>
    <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
      {onFinish && <button type="button" className="mcw-progress-button" onClick={onFinish}>{review.settlement_count ? "查看业主账单与交付文件" : "查看结算条件"}</button>}
      <button className="mcw-progress-button" onClick={onFiles}>查看全部资料（{fileCount} 份）</button>
    </div>
  </section>;
}
