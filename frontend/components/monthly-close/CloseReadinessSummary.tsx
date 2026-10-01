"use client";

import { Button } from "antd";
import type { MonthlyCloseIssue, MonthlyCloseStep } from "@/lib/monthly-close";
import { downloadBlob } from "@/lib/utils";
import { evidenceFilename } from "@/lib/monthly-close-experience";

const guidance: Record<string, { who: string; next: string }> = {
  source_collection: { who: "收支经办人", next: "先核对已收文件；缺原件时补充原件，已入账时关联已有记录。" },
  order_integrity: { who: "订单经办人", next: "按客人、房间和日期找到原订单，补充缺少的真实信息；不要用系统编号代替平台订单号。" },
  service_fees: { who: "保洁或运营负责人", next: "核实实际服务与审批情况，再检查已有费用，避免漏记或重复记账。" },
  utilities: { who: "费用经办人", next: "先核对已有支出，再确认原件金额、所属月份和承担方；同一笔费用只记一次。" },
  ota_statements: { who: "平台对账经办人", next: "核对平台订单与结算批次，区分到账时间和订单所属月份。" },
};

function cleanMessage(message: string) {
  return message.replace(/(?:[a-f\d]{24,64}\.(?:png|jpe?g|webp)\s*[：:]\s*)+/gi, "");
}

const labels: Record<string, string> = {
  source_collection: "公司收支资料", order_integrity: "订单资料",
  service_fees: "保洁与服务费记录", utilities: "水电与运营费用",
  ota_statements: "平台账单", exception_clearance: "其他待核对事项",
  preflight: "结算检查", settlement_review: "结算复核", owner_confirmation: "业主确认",
};

type IssueGroup = { key: string; title: string; stepKey: string; image: boolean; issues: MonthlyCloseIssue[] };

/** Group presentation only. Never change the server's close conditions or evidence. */
export function closeReadinessGroups(steps: MonthlyCloseStep[]): IssueGroup[] {
  const groups = new Map<string, IssueGroup>();
  const seen = new Set<string>();
  // The exception stage repeats issues from source stages. Prefer their direct handling location.
  const ordered = [...steps].sort((a, b) => Number(a.step_key === "exception_clearance") - Number(b.step_key === "exception_clearance"));
  for (const step of ordered.filter((item) => item.status !== "confirmed")) {
    for (const issue of step.issues ?? []) {
      const identity = JSON.stringify([issue.code, issue.source_id ?? "", issue.fact_key ?? issue.resource_id ?? issue.message]);
      if (seen.has(identity)) continue;
      seen.add(identity);
      const image = issue.code === "image_amount_unconfirmed";
      const source = issue.source_id || issue.document_id || issue.subject || issue.resource_id;
      const key = image ? `image:${source}` : step.step_key;
      const group = groups.get(key) ?? { key, title: image ? "费用图片待核对" : labels[step.step_key] ?? step.label, stepKey: step.step_key, image, issues: [] };
      group.issues.push(issue);
      groups.set(key, group);
    }
  }
  return Array.from(groups.values());
}

export function CloseReadinessSummary({ steps, onOpenStep, month }: {
  steps: MonthlyCloseStep[]; onOpenStep: (stepKey: string) => void; month?: string;
}) {
  const groups = closeReadinessGroups(steps);
  const pendingSteps = steps.filter((step) => step.status !== "confirmed");
  return <div aria-label="公司月结待办" style={{ display: "grid", gap: 16 }}>
    <p style={{ margin: 0 }}>这里只影响公司整月关账。已确认的业主结算保留；如需调整账单，应另走结算更正流程。</p>
    {groups.length > 0 && <Button onClick={() => downloadBlob(new Blob([`${month ?? "本月"}待核实事项\n以导出时状态为准，处理前重新检查。\n\n` + groups.map(group => `${group.title}\n建议核实人：${guidance[group.stepKey]?.who ?? "管理员"}\n${group.issues.map((issue, index) => `${index + 1}. ${evidenceFilename(issue.subject ?? "相关事项")}\n问题：${cleanMessage(issue.message)}\n影响：${issue.impact ?? "需核实后才能完成公司月结；金额影响尚未确定"}\n处理：${issue.next_step ?? guidance[group.stepKey]?.next ?? "核对相关业务依据"}\n核实结果：________\n`).join("\n")}`).join("\n")], { type: "text/plain;charset=utf-8" }), `${month ?? "本月"}-待核实清单.txt`)}>下载给同事核实的清单</Button>}
    {groups.map((group) => <div key={group.key} style={{ borderBottom: "1px solid var(--linen)", paddingBottom: 16 }}>
      <div style={{ fontWeight: 500 }}>{group.title} · {group.issues.length} {group.image ? "处金额线索" : "项"}</div>
      <p style={{ margin: "8px 0" }}>建议由{guidance[group.stepKey]?.who ?? "管理员"}核实。{guidance[group.stepKey]?.next ?? "查看具体问题和业务依据，核实后由管理员处理。"}</p>
      {group.image ? <p>同一张图片的金额已合并展示。这些是待核对的线索，不代表新增支出；应先与已入账费用关联，避免重复记账。</p> : null}
      {group.image && month && <Button style={{ minHeight: 44 }} href={`/finance?month=${encodeURIComponent(month)}&tab=expenses&returnTo=${encodeURIComponent(`/finance/monthly-close?month=${month}`)}`}>先查看本月已有支出</Button>}
      <details style={{ margin: "8px 0" }}>
        <summary style={{ cursor: "pointer", minHeight: 44, display: "flex", alignItems: "center" }}>查看具体原因</summary>
        <ul style={{ margin: "0 0 12px", paddingLeft: 20 }}>
          {group.issues.map((issue, index) => {
            // Storage filenames are not useful instructions. Keep the original evidence in its review screen.
            const message = cleanMessage(issue.message);
            const subject = issue.subject && !/^[a-f\d]{24,64}\.(?:png|jpe?g|webp)$/i.test(issue.subject) && !message.startsWith(issue.subject) ? `${issue.subject}：` : "";
            return <li key={index} style={{ marginBottom: 12, overflowWrap: "anywhere" }}>
              <div>{subject}{message}</div>
              <div>影响：{issue.impact ?? "需核实后才能完成公司月结；金额影响尚未确定"}</div>
              {issue.next_step && <div>处理方式：{issue.next_step}</div>}
            </li>;
          })}
        </ul>
      </details>
      <Button style={{ minHeight: 44 }} onClick={() => onOpenStep(group.stepKey)}>处理{labels[group.stepKey] ?? group.title}</Button>
    </div>)}
    {!groups.length && <><p>还需检查流程状态。已保存的账单和核对记录会继续保留。</p><Button onClick={() => onOpenStep(pendingSteps[0]?.step_key ?? "exception_clearance")}>查看待确认事项</Button></>}
    {groups.length > 0 && <p style={{ color: "var(--stone)", margin: 0 }}>同一问题在不同步骤中的重复提示已合并。以上事项处理后，系统仍会检查全部关账条件。</p>}
  </div>;
}
