"use client";
import { useState } from "react";
import type { AssistantReply, MonthlyCloseProjection } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";
export function FormattedResult({ reply, projection }: { reply: AssistantReply; projection: MonthlyCloseProjection }) {
  const output = reply.output;
  const [listMode, setListMode] = useState(output?.presentation.style === "checklist");
  const [pages, setPages] = useState<Record<number, number>>({});
  if (!output || projection.actor_role !== "admin" || output.document.billing_month !== projection.billing_month || reply.facts.billing_month !== projection.billing_month) return null;
  const { document, presentation } = output;
  const summary = presentation.style === "summary";
  const width = (column: string) => column === "序号" ? 56 : /日期|时间/.test(column) ? 112 : /房间|笔数/.test(column) ? 88 : /原因|依据|下一步|说明/.test(column) ? 240 : 160;
  const rows = <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", minWidth: 0, gap: 20 }}>
    <button type="button" onClick={() => setListMode(!listMode)} style={{ justifySelf: "start", minHeight: 44, padding: "0 14px", borderRadius: 999, border: `1px solid ${tokens.anyu.color.linen}`, background: tokens.anyu.color.shell, color: tokens.anyu.color.ink.default, font: "inherit", cursor: "pointer" }}>{listMode ? "切换为表格" : "逐条查看"}</button>
    {!listMode && document.sections.some(section => section.columns.length > 3) && <div style={{ fontSize: 12, color: tokens.anyu.color.stone }}>表格较宽，可左右滑动查看完整内容，也可切换为逐条查看。</div>}
    {document.sections.map((section, sectionIndex) => {
      const pageCount = Math.max(1, Math.ceil(section.rows.length / 50));
      const page = Math.min(pages[sectionIndex] ?? 0, pageCount - 1);
      const start = page * 50;
      const visibleRows = section.rows.slice(start, start + 50);
      const buttonStyle = { minHeight: 44, padding: "0 14px", borderRadius: 999, border: `1px solid ${tokens.anyu.color.linen}`, background: tokens.anyu.color.shell, color: tokens.anyu.color.ink.default, font: "inherit" };
      return <section key={sectionIndex} aria-label={section.title} style={{ minWidth: 0 }}>
      <div style={{ fontWeight: 500, marginBottom: 8 }}>{section.title}</div>
      {section.note && <p style={{ color: tokens.anyu.color.stone }}>{section.note}</p>}
      {pageCount > 1 && <nav aria-label={`${section.title}分页`} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12, marginBottom: 12 }}>
        <button type="button" style={buttonStyle} disabled={page === 0} onClick={() => setPages(previous => ({ ...previous, [sectionIndex]: page - 1 }))}>上一页</button>
        <span aria-live="polite" style={{ color: tokens.anyu.color.stone }}>第 {start + 1}–{Math.min(start + 50, section.rows.length)} 条，共 {section.rows.length} 条</span>
        <button type="button" style={buttonStyle} disabled={page === pageCount - 1} onClick={() => setPages(previous => ({ ...previous, [sectionIndex]: page + 1 }))}>下一页</button>
        <span style={{ fontSize: 12, color: tokens.anyu.color.stone }}>下载保留全部明细</span>
      </nav>}
      {listMode ? <ol start={start + 1} style={{ paddingLeft: 22, display: "grid", gap: 16 }}>{visibleRows.map((row, index) => <li key={start + index}>{row.map((value, column) => <div key={column}>{section.columns[column]}：{value ?? "待确认"}</div>)}</li>)}</ol> : <div role="region" aria-label={`${section.title}表格`} tabIndex={0} style={{ overflowX: "auto", maxWidth: "100%" }}><table style={{ width: "100%", borderCollapse: "collapse", textAlign: "left", fontSize: 14 }}>
        <thead><tr>{section.columns.map((col, index) => <th key={index} scope="col" style={{ padding: 10, minWidth: width(col), background: tokens.anyu.color.shell, borderBottom: `1px solid ${tokens.anyu.color.linen}`, fontWeight: 500 }}>{col}</th>)}</tr></thead>
        <tbody>{visibleRows.map((row, index) => <tr key={start + index}>{row.map((value, column) => <td key={column} style={{ padding: 10, minWidth: width(section.columns[column]), verticalAlign: "top", borderBottom: `1px solid ${tokens.anyu.color.linen}`, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{value ?? "待确认"}</td>)}</tr>)}</tbody>
      </table></div>}
    </section>;})}
  </div>;
  return <section aria-label="按需整理的核对结果" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12, minWidth: 0 }}>
    <div style={{ whiteSpace: "pre-wrap" }}>{document.summary}</div>
    {summary ? <details><summary style={{ minHeight: 44, lineHeight: "44px", cursor: "pointer" }}>展开依据与明细</summary>{rows}</details> : rows}
    <div style={{ color: tokens.anyu.color.stone, fontSize: 12, overflowWrap: "anywhere" }}>{document.caveats.map((line, i) => <div key={i}>{line}</div>)}</div>
  </section>;
}
