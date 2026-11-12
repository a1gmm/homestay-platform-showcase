"use client";

import { useState } from "react";
import type { MonthlyCloseProjection } from "@/lib/monthly-close";
import type { ReviewQuestion, ReviewQueue } from "@/lib/monthly-close-review";
import { extractErrorMessage } from "@/lib/api-errors";
import styles from "./ThreeStepReview.module.css";

interface Props {
  projection: MonthlyCloseProjection; queue?: ReviewQueue; loading: boolean; error: boolean;
  fileCount: number; busy: boolean; readOnly: boolean;
  onRefresh: () => void; onUpload: () => void; onFiles: () => void;
  onBills: () => void; onCheck: (question: ReviewQuestion) => void;
  onAnswer: (question: ReviewQuestion, answer: string) => Promise<void>;
}

function QuestionCard({ question, busy, readOnly, checking, onAnswer, onCheck }: Pick<Props, "busy" | "readOnly" | "onAnswer" | "onCheck"> & { question: ReviewQuestion; checking: boolean }) {
  const [answer, setAnswer] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const needsAction = ["document_review", "expense_not_posted", "bill_drift", "disputed_bill", "check_unavailable", "execution_pending", "offline_more"].includes(question.code);
  async function submit(value = answer.trim()) {
    if (!value || busy || sending || readOnly || checking) return;
    setSending(true); setError(null);
    try { await onAnswer(question, value); setSubmitted(true); }
    catch (error) { setError(extractErrorMessage(error, "补充没有提交成功，内容已保留，请重试。")); }
    finally { setSending(false); }
  }
  return <article className={styles.card} aria-label={`${question.title} ${question.subject}`}>
    <div className={styles.eyebrow}>{question.group} · 待核实</div>
    <h3>{question.title}</h3>
    <p className={styles.subject}>{question.subject}{question.amount != null && <span> · ¥{question.amount}</span>}</p>
    <p>{question.message}</p>
    {question.source_name && <p className={styles.source}>依据：{question.source_name}{question.source_row_label ? ` · ${question.source_row_label}` : ""}</p>}
    {!needsAction && <>{question.suggestions.length > 0 && <div className={styles.choices} aria-label="可选实际情况">{question.suggestions.map(text =>
      <button type="button" key={text} disabled={busy || sending || readOnly} onClick={() => { setAnswer(text); setSubmitted(false); }}>{text}</button>)}</div>}
    <label htmlFor={`answer-${question.question_id}`}>补充实际情况</label>
    <textarea id={`answer-${question.question_id}`} value={answer} maxLength={1800} rows={2} disabled={busy || sending || readOnly}
      placeholder="例如：实际已收109元，应收也为109元；不清楚可以先保留。"
      onChange={event => { setAnswer(event.target.value); setSubmitted(false); }} />
    </>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {submitted && <p role="status">补充已提交，正在按实际结果复核。需要改账时，请继续查看并确认处理方案；这项仍保留在待核清单。</p>}
    <div className={styles.actions}>
      {!needsAction && <><button type="button" className={styles.primary} disabled={!answer.trim() || busy || sending || readOnly || checking} onClick={() => void submit()}>{sending ? "正在提交…" : "提交补充"}</button>
        <button type="button" disabled={busy || sending || readOnly} onClick={() => { setAnswer("暂时不清楚，请保留待核实，不修改账目。"); setSubmitted(false); }}>暂时不清楚</button></>}
      {question.code === "expense_not_posted" ? <button type="button" className={styles.primary} disabled={busy || sending || readOnly || checking} onClick={() => void submit("生成记账方案")}>查看这笔费用的记账方案</button>
        : <button type="button" className={needsAction ? styles.primary : undefined} disabled={busy || sending} onClick={() => onCheck(question)}>查看依据 / 处理方案</button>}
    </div>
  </article>;
}

export function ThreeStepReview(props: Props) {
  const { projection, fileCount, busy, readOnly, onRefresh, onUpload, onFiles, onBills } = props;
  const queue = props.queue?.cycle_id === projection.cycle_id && props.queue.billing_month === projection.billing_month ? props.queue : undefined;
  const current = !fileCount ? 1 : !queue || queue.pending_count ? 2 : 3;
  const [selection, setSelection] = useState<number | null>(null);
  const selected = selection ?? current;
  const ready = !!queue && !props.error && !props.loading;
  const review = projection.final_review;
  const completed = review.state === "verified";
  return <section className={styles.flow} aria-label="月结三步主流程">
    <div className={styles.heading}><div><p className={styles.eyebrow}>{projection.billing_month} · 管理员工作台</p><h2>{completed ? `${projection.billing_month} 业主对账已完成` : "本月对账，按这三步完成"}</h2></div><button type="button" onClick={onRefresh} disabled={props.loading || busy}>{props.loading ? "正在核查…" : "刷新核查"}</button></div>
    <nav className={styles.steps} aria-label="对账步骤">
      {["交资料", "回答待核问题", "查看业主账单"].map((label, index) => <button type="button" key={label}
        aria-current={selected === index + 1 ? "step" : undefined} onClick={() => setSelection(index + 1)}>
        <span className={styles.number}>{index + 1}</span><span>{label}<small>{index === 0 ? `已保存 ${fileCount} 份` : index === 1 ? ready ? `${queue!.pending_count} 项待核实` : "正在取得核查结果" : `已生成 ${review.settlement_count} 份`}</small></span>
      </button>)}
    </nav>
    <div className={styles.panel}>
      {selected === 1 && <><h3>像发微信一样，把本月资料交过来</h3><p>平台账单、保洁表、水电表可一起上传。临时支出直接补充一句，已有资料不必重复交。</p>
        <div className={styles.actions}><button type="button" className={styles.primary} onClick={onUpload} disabled={busy || readOnly}>添加本月资料</button><button type="button" onClick={onFiles}>查看已交资料（{fileCount} 份）</button>{fileCount > 0 && <button type="button" onClick={() => setSelection(2)}>继续核对问题 →</button>}</div>
        <p className={styles.source}>原件保存后，点击“开始核对这些资料”。识别失败的文件会单独提示，成功的文件会保留。</p></>}
      {selected === 2 && <><h3>统一待核清单{ready ? ` · ${queue!.pending_count} 项` : ""}</h3><p>按房间、日期和金额逐项补充。暂时不清楚的留在这里，不需要重新上传整批资料。</p>
        {props.error && <p role="alert" className={styles.error}>待核清单暂未更新，不能当作已经对平。已有输入保留，请点击“刷新核查”重试后再提交。</p>}
        {!queue ? !props.error && <p role="status">正在核查当前订单、原件、收款和费用…</p> : <>
          {props.loading && <p role="status">正在更新，以下是上次核查结果…</p>}
          {queue.questions.map(question => <QuestionCard key={question.question_id + question.evidence_hash} question={question} {...props} checking={props.loading || props.error} />)}
          {!queue.pending_count && ready && <div className={styles.clear}><h3>{fileCount ? "当前未发现影响业主账单的待核问题" : "先交本月资料，再开始核对"}</h3><p>{fileCount ? "已回答的事项按当前业务记录重新检查。仍可补充资料或说明，依据变化后会重新提示。" : "目前没有原始资料，空清单不代表已经对账完成。"}</p><button type="button" className={styles.primary} onClick={() => setSelection(fileCount ? 3 : 1)} disabled={props.loading}>{fileCount ? "继续查看业主账单" : "去交资料"}</button></div>}
        </>}
      </>}
      {selected === 3 && <><h3>{review.settlement_count ? "业主账单与交付文件" : "核对完成后，生成业主账单"}</h3>
        <p>已生成 {review.settlement_count} 份 · 已确认 {review.confirmed_settlement_count ?? 0} 份</p>
        {review.settlement_count > 0 && <p className={styles.total}>业主合计应付 <strong>¥{review.settlement_total_amount}</strong></p>}
        {(!ready || !!queue?.pending_count) && <p role="status">{ready ? `还有 ${queue!.pending_count} 项待核实，请先处理；已有账单可以查看。` : "待核清单正在核查，当前不能据此判断最终账单已完成。"}</p>}
        {!fileCount && <p>本月尚未交原始资料，请先完成资料核对。</p>}
        <div className={styles.actions}><button type="button" className={styles.primary} onClick={onBills} disabled={busy}>{review.settlement_count ? "查看账单与完成对账" : "打开账单检查与生成"}</button>{!!queue?.pending_count && <button type="button" onClick={() => setSelection(2)}>返回待核清单</button>}</div>
        <p className={styles.source}>核对并确认全部业主账单后，点击“完成本月业主对账”。这里不会自动登记打款，实际打款按实际情况另行登记。</p>
      </>}
    </div>
  </section>;
}
