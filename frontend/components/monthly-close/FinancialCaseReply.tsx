"use client";
import { useState } from "react";
import { monthlyCloseApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import type { AssistantReply, FinancialCaseFacts, FinancialCaseIssue, MonthlyCloseProjection } from "@/lib/monthly-close";
import { downloadBlob } from "@/lib/utils";
import styles from "./CleaningWorkChatReply.module.css";

export function FinancialCaseReply({ reply, projection, onFollowUp, onPrepareReply, onReviewDocument, busy = false, historical = false }: {
  reply: AssistantReply; projection: MonthlyCloseProjection;
  onFollowUp?: (text: string, contextRunId: string, attachmentIds?: string[]) => void;
  onPrepareReply?: (question: string, contextRunId: string) => void;
  onReviewDocument?: (documentId: string) => void;
  busy?: boolean; historical?: boolean;
}) {
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exportNotice, setExportNotice] = useState<string | null>(null);
  if (reply.tool !== "financial_case" || reply.facts.projection_version !== "financial-case-v1"
    || projection.actor_role !== "admin" || reply.facts.billing_month !== projection.billing_month) return null;
  const facts = reply.facts as unknown as FinancialCaseFacts;
  const recognitionUpdated = !historical && !facts.ledger_scope && !facts.order_scope && facts.sources.some((source) => {
    const current = projection.financial_case?.sources.find((item) => item.source_id === source.source_id);
    return current && current.fact_count !== source.fact_count;
  });
  historical = historical || recognitionUpdated;
  const valueText = (value: unknown) => value == null || typeof value === "string" && !value.trim() ? "待确认" : String(value);
  const visibleIssues = facts.issues.filter((issue) => issue.code !== "no_source_facts" || !facts.issues.some(
    (other) => other.code === "cleaning_parse_error" && other.source_id === issue.source_id));
  const postableCount = facts.metrics.find((metric) => metric.label === "可新增支出")?.value;
  const hasPostableExpenses = postableCount !== undefined && Number(postableCount) > 0;
  const issueMessage = (issue: FinancialCaseIssue) => ({
    cleaning_parse_error: /第\d+行/.test(issue.message) ? issue.message : "这份保洁表还没读出打扫记录，日期或房号没有识别成功。请打开原表，核对日期和房号所在的列。",
    no_source_facts: "文件已保存，但还没读出可核对的明细。请补充表格说明或清晰原表，暂时不能据此记账。",
    cleaning_evidence_only: "已读到打扫日期、房间和类型。下一步核对系统是否漏记或重复；打扫次数不能直接当作收费金额。",
  }[issue.code] ?? issue.message);
  const renderIssue = (issue: FinancialCaseIssue, index: number) => {
    const source = facts.sources.find((source) => source.source_id === issue.source_id);
    const linkedId = projection.financial_case?.sources.find((item) => item.source_id === issue.source_id)?.document_id;
    const documentId = linkedId && projection.sources.some((item) => item.documents.some((doc) => doc.document_id === linkedId)) ? linkedId : undefined;
    const question = `${source ? `《${source.filename}》：` : ""}${issueMessage(issue)}`;
    return <li key={`${issue.code}-${index}`}>
      {source && <div>来源：{source.filename}</div>}
      {!source && issue.source_id && <div>来源：相关原始资料</div>}
      <p style={{ margin: "4px 0", whiteSpace: "pre-wrap" }}>{issueMessage(issue)}</p>
      {!historical && <div className={styles.actions}>
        {issue.code === "cleaning_parse_error" && documentId && onReviewDocument
          ? <button type="button" disabled={busy} onClick={() => onReviewDocument(documentId)}>查看原表并确认列</button>
          : issue.code === "cleaning_evidence_only" && source
          ? <button type="button" disabled={busy || !onFollowUp} onClick={() => follow("核对保洁记录", [source.source_id])}>核对打扫记录</button>
          : onPrepareReply && <button type="button" disabled={busy} onClick={() => onPrepareReply(question, reply.run_id)}>补充这项信息</button>}
      </div>}
    </li>;
  };
  const isInterpretation = facts.proposal_kind === "interpretation";
  const reportScope = facts.report_months.length === 1 ? facts.report_months[0]
    : facts.report_months.length > 1 ? `${facts.report_months[0]}至${facts.report_months.at(-1)}-所选月份` : null;
  const follow = (text: string, attachmentIds?: string[]) => attachmentIds ? onFollowUp?.(text, reply.run_id, attachmentIds) : onFollowUp?.(text, reply.run_id);
  const exportReport = async () => {
    if (exporting || !reportScope) return;
    setExporting(true); setExportError(null); setExportNotice(null);
    try {
      const response = await monthlyCloseApi.exportFinancialCase(projection.billing_month, facts.report_months);
      downloadBlob(response.data, `${reportScope}-利润报告.xlsx`);
      setExportNotice("已下载当前所选月份的报告；是否可定稿请看报告中的待确认事项。");
    } catch (error) { setExportError(extractErrorMessage(error, "利润报告下载失败，请重试")); }
    finally { setExporting(false); }
  };
  const exportChecklist = async () => {
    setExporting(true); setExportError(null);
    try {
      const response = await monthlyCloseApi.exportReviewChecklist(projection.billing_month);
      downloadBlob(response.data, `${projection.billing_month}结算待核实清单.docx`);
      setExportNotice("核实清单已下载。同事填好每项的答案栏后，可以直接拖回聊天框。");
    } catch (error) { setExportError(extractErrorMessage(error, "核实清单下载失败，请重试")); }
    finally { setExporting(false); }
  };
  if (facts.order_scope && facts.state !== "proposal" && facts.state !== "completed") return <div className={styles.reply} aria-label="订单核实结果">
    <p className={styles.caption}>{facts.message}</p>
    <ol className={styles.list}>{facts.details.map((detail, index) => <li key={index}>
      <div>{detail.label}</div><p>{detail.value}</p>
    </li>)}</ol>
    {historical ? <p>这是此前查询的范围，处理前会重新核对是否有变化。</p> : <>
      {facts.order_scope.order_ids.length > 0 && <p>可在聊天中说明“这些订单怎么处理”，也可以补充客人或房号缩小范围。修改前会列出具体方案。</p>}
      <div className={styles.actions}><button type="button" disabled={busy || exporting} onClick={() => void exportChecklist()}>下载待核实清单 Word</button></div>
    </>}
    {exportError && <div role="alert">{exportError}</div>}
    {exportNotice && <div role="status">{exportNotice}</div>}
  </div>;
  if (facts.ledger_scope) return <div className={styles.reply} aria-label="费用查询结果">
    <p className={styles.caption}>{facts.message}</p>
    <details><summary>查看 {facts.details.length} 笔费用与依据</summary>
      <ul className={styles.list}>{facts.details.map((detail, index) => <li key={index}>
        <div>{detail.label}</div><p>{detail.value}</p>
      </li>)}</ul>
    </details>
    <details><summary>更多操作</summary><div className={styles.actions}>
      <button type="button" onClick={() => downloadBlob(new Blob([facts.message, "\n\n", ...facts.details.map((detail) => `${detail.label}\n${detail.value}\n\n`)], { type: "text/plain;charset=utf-8" }), `${projection.billing_month}费用查询明细.txt`)}>下载本次查询明细</button>
      {!historical && <button type="button" disabled={busy || exporting} onClick={() => void exportChecklist()}>下载待核实清单 Word</button>}
    </div></details>
    {historical && <p>这是此前查询的结果，重新提问可查询最新账目。</p>}
    {exportError && <div role="alert">{exportError}</div>}
    {exportNotice && <div role="status">{exportNotice}</div>}
  </div>;
  return <div className={styles.reply} aria-label="资料对账结果">
    {recognitionUpdated && <div role="status">
      <p>资料已重新识别。下面是上次回复，请重新核对查看最新问题。</p>
      <div className={styles.actions}><button type="button" disabled={busy || !onFollowUp} onClick={() => follow("核对这些资料")}>查看最新核对结果</button></div>
    </div>}
    <p className={styles.caption} style={{ whiteSpace: "pre-wrap" }}>{facts.message || reply.message}</p>
    {visibleIssues.length > 0 && <section aria-label="需要你处理的问题">
      <p className={styles.caption}>{historical ? "当时尚待核对" : "下一步，先处理这里的问题"}（{visibleIssues.length} 项）</p>
      <ol className={styles.list} style={{ marginTop: 12 }}>{visibleIssues.slice(0, 3).map(renderIssue)}</ol>
      {visibleIssues.length > 3 && <details><summary>查看其余 {visibleIssues.length - 3} 项问题</summary>
        <ol className={styles.list} start={4}>{visibleIssues.slice(3).map((issue, index) => renderIssue(issue, index + 3))}</ol>
      </details>}
      {!historical && <p>知道情况就点“补充这项信息”回复；不清楚的可以说“待核实”。需要查看原表时，先按对应按钮打开。</p>}
    </section>}
    {facts.metrics.length > 0 && <details open={visibleIssues.length === 0 || facts.state === "proposal" ? true : undefined}>
      <summary>查看汇总数字</summary>
      {facts.metrics.some((metric) => metric.label === "本月系统已记支出") && <p>“系统已记支出”是系统里已有的账，不代表这次上传的费用已经入账。对应记录为 0，也不代表没有发生费用。</p>}
      <dl className={styles.metrics}>{facts.metrics.map((metric, index) => <div key={`${metric.label}-${index}`}>
        <dt>{metric.label}</dt><dd>{valueText(metric.value)}{metric.detail && <small>{metric.detail}</small>}</dd>
      </div>)}</dl>
    </details>}
    {facts.sources.length > 0 && <details>
      <summary>查看 {facts.sources.length} 份原始资料</summary>
      <p>原件已保存，归档不代表费用已入账。图片中的金额只作候选，需核实用途、月份、承担方和实际付款方。</p>
      <ul className={styles.list}>{facts.sources.map((source) => <li key={source.source_id}>
        <div>{source.filename}</div><small>{source.kind === "checklist" ? "系统导出的待核实清单" : source.kind === "feedback" ? "同事回填的核实依据" : source.kind === "image" ? "图片文字待核实" : source.fact_count > 0 ? `${source.fact_count} 条来源明细，计入方式待核对` : "未识别到明细，请补充原表或说明"}{source.kind === "image" && source.fact_count === 0 ? " · 请补充文字或 Excel" : ""}</small>
      </li>)}</ul>
    </details>}
    {facts.sources.length === 0 && !facts.issues.some((issue) => issue.code === "pending_review") && facts.proposal_kind !== "business_correction" && <p>还没有可供核对的原始资料。请在下方添加 Excel 或截图，并说明要核对的月份。</p>}
    {facts.details.length > 0 && <details open={facts.state === "proposal" ? true : undefined}>
      <summary>{facts.state === "proposal" ? isInterpretation ? "查看待保存的来源解释" : "查看拟执行变更与依据" : "查看核对明细与依据"}</summary>
      <dl className={styles.metrics}>{facts.details.map((detail, index) => <div key={`${detail.label}-${index}`}><dt>{detail.label}</dt><dd>{valueText(detail.value)}</dd></div>)}</dl>
    </details>}
    {historical ? <p>这是历史结果，请以最新核对结果为准。</p> : <>
      {facts.state === "proposal" && facts.proposal_id ? <>
        <p>{isInterpretation ? "这一步只保存你对原件的解释。保存后，费用可继续生成记账方案；收入或资金往来可重算报告。" : facts.proposal_kind === "posting" ? "这一步会按上方方案新增系统支出。请逐笔核对所属月份、金额、房间、承担方和实际付款方。" : "请先查看方案说明和具体变更，确认按钮只执行当前展示的这份方案。"}</p>
        <div className={styles.actions}>
        <button type="button" className={styles.primary} disabled={busy || !onFollowUp} onClick={() => follow("确认执行")}>{isInterpretation ? "确认保存解释" : facts.proposal_kind === "posting" ? "确认记入系统支出" : "确认执行这份方案"}</button>
        <button type="button" disabled={busy || !onFollowUp} onClick={() => follow("取消方案")}>先不执行</button>
      </div></> : <>
        {facts.state === "completed" && isInterpretation && <p>来源解释已保存，系统支出尚未新增。费用请继续生成记账方案；只核对收入或资金往来时可直接生成报告。</p>}
        <div className={styles.actions}>
        {facts.state === "needs_information" ? <span>请按上方问题回复“文件名／原表行或房间＋需要补充的信息”；金额或付款情况不清楚时可直接说“待核实”。</span> : <>
          <button type="button" disabled={busy || !onFollowUp} onClick={() => follow("核对这些资料")}>重新核对资料</button>
          {(facts.state !== "completed" || isInterpretation) && (hasPostableExpenses || isInterpretation && facts.state === "completed") && <button type="button" className={styles.primary} disabled={busy || !onFollowUp} onClick={() => follow("生成记账方案")}>查看待入账费用</button>}
          {facts.state === "completed" && <button type="button" disabled={busy || !onFollowUp} onClick={() => follow("生成利润报告")}>生成利润报告</button>}
        </>}
      </div></>}
      {postableCount !== undefined && Number(postableCount) === 0 && facts.state !== "proposal" && <p>目前没有可以直接入账的明细。先处理上面的问题，或重新核对资料；这不表示本月费用为零。</p>}
      {facts.export_ready && <div className={styles.actions}>
        <button type="button" disabled={busy || exporting || !reportScope} onClick={() => void exportReport()}>{exporting ? "正在下载利润报告" : "下载利润报告 Excel"}</button>
        <span>{reportScope ? `报告月份：${facts.report_months.join("、")}` : "报告月份待确认，请先说明需要哪几个月的报告。"}</span>
        {facts.report_months.some((month) => month !== projection.billing_month) && <span>当前工作区为 {projection.billing_month}，报告按上述月份汇总；跨月记账需在方案中逐笔确认所属月份。</span>}
      </div>}
    </>}
    {!historical && <div className={styles.actions}>
      <button type="button" disabled={busy || exporting} onClick={() => void exportChecklist()}>下载待核实清单 Word</button>
    </div>}
    {exportError && <div role="alert">{exportError}</div>}
    {exportNotice && <div role="status">{exportNotice}</div>}
  </div>;
}
