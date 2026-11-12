"use client";
import type { MonthlyCloseProjection } from "@/lib/monthly-close";
import styles from "./MonthlyCloseWorkspace.module.css";

export function ReconciliationProgress({ projection, onOffline, onCheck }: {
  projection: MonthlyCloseProjection; onOffline?: () => void; onCheck?: () => void;
}) {
  const progress = projection.business_progress;
  if (projection.actor_role !== "admin" || !progress) return null;
  return <section className={styles.outcome} aria-label="当前账务核对状态">
    <h3>收款与费用核对</h3>
    <dl>
      <dt>账单基础检查</dt><dd style={{ margin: 0 }}>{progress.billing_check === "passed"
        ? "已登记的订单、平台差异及费用未发现阻断项，原表与实收仍需核对"
        : progress.billing_check === "needs_review" ? `${progress.billing_issue_count} 项需要处理，请先查看具体差异` : "检查结果暂不可用，请重新读取本月状态"}</dd>
      <dt>线下收款</dt><dd style={{ margin: 0 }}>{progress.offline_order_count
        ? `${progress.offline_order_count} 笔中 ${progress.offline_pending_count} 笔待核实` : "本月没有需核对的普通线下入住订单"}
        {progress.offline_confirmed_count > 0 && <p style={{ margin: "4px 0 0" }}>其中 {progress.offline_confirmed_count} 笔由人工确认实收，共 ¥{progress.offline_confirmed_amount}；收款日期、方式及凭据另核。</p>}</dd>
    </dl>
    {progress.new_materials_since_bills && <p role="status">账单生成后又收到了资料。请先核对新增内容，再判断是否需要重新生成账单。</p>}
    {progress.bill_drift_count > 0 && <p role="alert">{progress.bill_drift_count} 份待确认账单的订单或费用已变化。请核对变化后重新生成，再确认账单。</p>}
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {onOffline && <button type="button" className="mcw-progress-button" onClick={onOffline}>查看线下收款</button>}
      {onCheck && <button type="button" className="mcw-progress-button" onClick={onCheck}>查看账单检查</button>}
    </div>
    <p style={{ color: "var(--stone)", fontSize: 12 }}>检查通过不代表资料已齐全或业主已确认。公司银行收支、付款登记和正式关账分别处理。</p>
  </section>;
}
