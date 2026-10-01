import type { MonthlyCloseProjectionSource } from "@/lib/monthly-close";
import { presentSourceProgress } from "@/lib/monthly-close-presentation";
import { tokens } from "@/lib/design-tokens";

const SOURCE_LABELS: Record<string, string> = {
  ota_statement: "OTA 平台账单",
  cleaning_statement: "保洁打扫记录",
  linen_statement: "布草／洗涤记录",
  utility_receipt: "水电支出（历史记录）",
  utility_expense: "水电支出",
  operating_expenses: "其他运营支出",
  system_service_fees: "系统服务费",
};

const STATE_COLOR: Record<MonthlyCloseProjectionSource["state"], string> = {
  missing: tokens.anyu.color.driftwood,
  processing: tokens.anyu.color.clay,
  needs_action: tokens.anyu.color.clay,
  completed: tokens.anyu.color.sage,
  blocked: "#9B4A43",
  not_applicable: tokens.anyu.color.stone,
};

export function sourceLabel(sourceType: string) {
  return SOURCE_LABELS[sourceType] ?? "其他资料";
}

export function SourceChecklist({
  sources,
  onMarkNotApplicable,
  receiptState,
}: {
  sources: MonthlyCloseProjectionSource[];
  receiptState?: (type: string) => string | null;
  onMarkNotApplicable?: (source: MonthlyCloseProjectionSource) => void;
}) {
  const visibleSources = sources.filter((source) => source.source_type !== "utility_receipt");
  return (
    <ul aria-label="本月资料清单" style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {visibleSources.map((source) => (
        <li key={source.source_id} style={{ display: "grid", gridTemplateColumns: "10px minmax(0, 1fr) auto", gap: 10, alignItems: "center", padding: "12px 4px", borderBottom: `0.5px solid ${tokens.anyu.color.linen}` }}>
          <span aria-hidden="true" style={{ width: 7, height: 7, borderRadius: "50%", marginTop: 7, background: STATE_COLOR[source.state] }} />
          <span style={{ minWidth: 0 }}>
            <span style={{ display: "block", color: tokens.anyu.color.ink.default }}>{sourceLabel(source.source_type)}</span>
            <span style={{ display: "block", color: tokens.anyu.color.stone, fontSize: 12, marginTop: 2 }}>{source.state === "missing" && receiptState?.(source.source_type) || presentSourceProgress(source)}</span>
          </span>
          {source.state === "missing" && !receiptState?.(source.source_type) && onMarkNotApplicable && (
            <button
              type="button"
              aria-label={`本月不需要「${sourceLabel(source.source_type)}」`}
              onClick={() => onMarkNotApplicable(source)}
              style={{ minHeight: 44, border: `1px solid ${tokens.anyu.color.linen}`, borderRadius: 999, padding: "0 12px", background: tokens.anyu.color.shell, color: tokens.anyu.color.stone, font: "inherit", fontSize: 12 }}
            >
              本月不需要
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}
