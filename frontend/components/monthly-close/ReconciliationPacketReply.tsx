"use client";

import Link from "next/link";
import type { FinancialCaseFacts } from "@/lib/monthly-close";
import { downloadBlob } from "@/lib/utils";
import styles from "./CleaningWorkChatReply.module.css";

export function ReconciliationPacketReply({ facts, historical, busy, onRefresh, onReview, onPrepare }: {
  facts: FinancialCaseFacts; historical: boolean; busy: boolean;
  onRefresh?: () => void; onReview?: (id: string) => void;
  onPrepare?: (question: string, answer?: string) => void;
}) {
  const packet = facts.packet!;
  const platformSources = packet.sources.filter((source) => source.kind === "ota");
  const feeSources = packet.sources.filter((source) => source.kind !== "ota");
  const questionCount = packet.questions.reduce((sum, question) => sum + question.count, 0);
  const sourceFor = (id: string | null) => packet.sources.find((s) => s.source_id === id);
  const questionText = (question: typeof packet.questions[number]) => {
    const source = sourceFor(question.source_id);
    if (source?.kind === "note") return "这笔补充费用请说明：发生日期、房间、各项用途和金额、谁承担、谁实际付款。若包含打扫和电费，请说明各自金额；数字含义不清楚的请保留待核实。";
    return question.title;
  };
  const handoffText = [
    `${facts.billing_month} 对账交接清单（尚未完成核对）`,
    ...packet.sources.map((s) => `${s.label}｜${s.filename}\n${s.summary}\n${s.note}${s.other_version_count ? "\n存在同名其他版本，请核实本批使用的原件。" : ""}`),
    `线下房费｜系统本月离店 ${packet.offline.count} 单，${packet.offline.pending_count} 单金额需核实\n${packet.offline.note}`,
    ...packet.questions.map((q) => `${sourceFor(q.source_id)?.filename ?? "本批资料"}｜${questionText(q)}（${q.count} 项）\n${q.examples.join("\n")}\n答复：待核实`),
    ...packet.notes.map((note) => `交接时补充的原话：${note}\n当前处理状态以对应费用说明和系统支出为准。`),
  ].join("\n\n");
  const sourceList = (sources: typeof packet.sources) => <ul className={styles.list}>
      {sources.map((source) => <li key={source.source_id}>
        <div>{source.label} · {source.filename}</div>
        <p>{source.summary}</p><p>{source.note}</p>
        {source.other_version_count > 0 && <p role="status">另有 {source.other_version_count} 份同名不同内容的版本。此处只显示本批选中的原件；请先核实应使用哪版，未自动合并或删除旧版。</p>}
        {!historical && source.document_id && onReview && <div className={styles.actions}>
          <button type="button" disabled={busy} onClick={() => onReview(source.document_id!)}>
            {source.kind === "cleaning" ? "核对这份打扫记录" : source.kind === "ota" ? "核对这份平台账单" : "核对这份费用明细"}
          </button>
        </div>}
      </li>)}
    </ul>;
  return <section className={styles.reply} aria-label="本批资料核对清单">
    <p>{facts.message}</p>
    <p>先对平平台、线下收款和各项费用，再生成业主账单。按下面的顺序逐项核对即可。</p>
    {packet.excluded_source_count > 0 && <p>本次只核对选中的资料，工作区另有 {packet.excluded_source_count} 份历史资料未混入本批。</p>}
    <section aria-label="平台账单核对">
      <h3>1. 核对平台账单</h3>
      {platformSources.length ? sourceList(platformSources) : <p>本批没有平台账单。如本月有平台订单，请补充对应原件。</p>}
    </section>
    <section aria-label="线下房费核对">
      <h3>2. 核对线下房费</h3>
      <p>系统中本月离店 {packet.offline.count} 单，{packet.offline.pending_count} 单金额需核实。</p>
      <p>{packet.sources.some((source) => source.kind === "utility") ? "本批水电已收表不能充当线下房费凭据。" : "系统登记的收款仍需与实际收款核对。"}请核对微信、支付宝或现金收款依据；表里没写不等于本月没有。</p>
      <details><summary>查看系统线下订单与收款</summary>
        <p>{packet.offline.note}</p>
        {packet.offline.rows.length === 0 ? <p>系统暂未查到该范围的订单，请核实月份与渠道，不能据此认定线下收入为零。</p> : <ul className={styles.list}>
          {packet.offline.rows.map((row) => <li key={row.order_id}>
            <div>{row.room} · {row.checkout} 离店 · {row.confirmed_received ? "实收金额已人工确认，流水待补" : row.needs_review ? "需核实" : "账面金额一致，凭据待核对"}</div>
            <p>订单金额 {row.amount ?? "待确认"} 元 · 已登记房费 {row.received} 元 · 房费退款 {row.refunded} 元 · 差额 {row.difference ?? "待确认"} 元</p>
            {row.confirmed_received && <p>管理员确认累计实收 {row.confirmed_received} 元。日期、方式和凭证待补；此确认不新增流水，不计入银行现金报告。</p>}
            {!historical && <Link href={`/orders?order_id=${encodeURIComponent(row.order_id)}`}>查看这笔订单</Link>}
          </li>)}
        </ul>}
        {packet.offline.truncated && <p>此处优先展示前 200 单，完整范围请到订单管理继续核对。</p>}
      </details>
    </section>
    <section aria-label="各项费用核对">
      <h3>3. 核对保洁、水电和其他费用</h3>
      {feeSources.length ? sourceList(feeSources) : <p>本批没有费用资料。如本月有保洁、水电或其他支出，请补充原件或说明。</p>}
    </section>
    <section aria-label="本批需要补充的情况">
      <h3>需要你补充的情况</h3>
      <p>{packet.questions.length ? `${questionCount} 项已按同一份资料、同一种原因整理成 ${packet.questions.length} 组。` : "暂未发现额外的来源问题；仍需完成上面的记录、金额和收款核对。"}</p>
      <ol className={styles.list}>{packet.questions.map((question, index) => <li key={`${question.source_id}-${question.code}`}>
        <div>{sourceFor(question.source_id)?.filename ?? "本批资料"} · {question.count} 项</div>
        <p>{questionText(question)}</p>
        <details><summary>查看问题举例</summary>{question.examples.map((example, i) => <p key={i}>{example}</p>)}{question.count > question.examples.length && <p>另有 {question.count - question.examples.length} 项，请从对应资料入口查看逐笔结果。</p>}</details>
        {!historical && onPrepare && <div className={styles.actions}>
          <button type="button" disabled={busy} onClick={() => onPrepare(`《${sourceFor(question.source_id)?.filename ?? "本批资料"}》：${questionText(question)}`)}>补充这组情况</button>
          <button type="button" disabled={busy} onClick={() => onPrepare(`第 ${index + 1} 组：${questionText(question)}`, "这组暂时无法确认，请保留待核实，不要入账。")}>暂时不知道</button>
        </div>}
      </li>)}</ol>
    </section>
    <section aria-label="业主账单下一步">
      <h3>核对完成后，再生成业主账单</h3>
      <p>先处理上面的差异和待核实情况，新增或调整的费用须查看方案并确认。完成后，到本月进度查看业主账单是否可以生成。</p>
    </section>
    {historical ? <p>这是当时这批资料的核对清单，实际处理请重新核对最新记录。</p> : <div className={styles.actions}>
      <button type="button" className={styles.primary} disabled={busy || !onRefresh} onClick={onRefresh}>刷新本批核对结果</button>
      <button type="button" onClick={() => downloadBlob(new Blob([handoffText], { type: "text/plain;charset=utf-8" }), `${facts.billing_month}-对账交接清单.txt`)}>下载本批交接清单</button>
    </div>}
    <p>识别、系统对应和完成对账是不同步骤。新增费用仍需查看方案并确认；这里不会自动记账或结束月份。</p>
  </section>;
}
