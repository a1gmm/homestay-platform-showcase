"use client";
import type { AssistantReply, MonthlyCloseProjection } from "@/lib/monthly-close";
import styles from "./CleaningWorkChatReply.module.css";

export function OrderIdentityChatReply({ reply, projection, onFollowUp, busy, historical = false }: {
  reply: AssistantReply; projection: MonthlyCloseProjection;
  onFollowUp?: (text: string, runId: string) => void; busy?: boolean; historical?: boolean;
}) {
  if (reply.tool !== "order_identity_chat" || projection.actor_role !== "admin" || reply.facts.billing_month !== projection.billing_month) return null;
  const facts = reply.facts;
  return <div className={styles.reply} aria-label="订单单号补充方案">
    {typeof facts.order_id === "string" && <p>系统订单：{facts.order_id}</p>}
    {typeof facts.platform_order_id === "string" && <p style={{ overflowWrap: "anywhere" }}>平台订单号：{facts.platform_order_id}</p>}
    {facts.state === "proposal" && (historical ? <p>历史方案，请查看最新处理结果。</p> : <div className={styles.actions}>
      <button type="button" className={styles.primary} disabled={busy || !onFollowUp} onClick={() => onFollowUp?.("确认执行", reply.run_id)}>确认补充这个单号</button>
      <button type="button" disabled={busy || !onFollowUp} onClick={() => onFollowUp?.("取消方案", reply.run_id)}>先不执行</button>
    </div>)}
    {(facts.state === "completed" || facts.state === "conflict") && <div className={styles.actions}><button type="button" disabled={busy || !onFollowUp} onClick={() => onFollowUp?.("订单完整性还有哪些问题，应该怎么处理", reply.run_id)}>重新核对订单</button></div>}
  </div>;
}
