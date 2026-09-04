import { CheckOutlined, LockOutlined } from "@ant-design/icons";
import type { MonthlyCloseStep } from "@/lib/monthly-close";
import { canProcessMonthlyCloseStepEarly } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";

const statusTone = {
  ready: tokens.anyu.color.ink.default,
  blocked: tokens.anyu.color.clay,
  locked: tokens.anyu.color.driftwood,
  confirmed: tokens.anyu.color.sage,
  stale: tokens.anyu.color.clay,
} as const;

export function CloseProgress({
  steps,
  currentStepKey,
  selectedStepKey,
  onSelect,
}: {
  steps: MonthlyCloseStep[];
  currentStepKey?: string | null;
  selectedStepKey?: string | null;
  onSelect?: (stepKey: string) => void;
}) {
  return (
    <nav className="monthly-close-steps" aria-label="月结九步" style={{ display: "flex", flexDirection: "column" }}>
      {steps.map((step) => {
        const selected = step.step_key === selectedStepKey;
        const canWorkAhead = step.status === "locked" && canProcessMonthlyCloseStepEarly(step.step_key);
        const isCurrent = step.step_key === currentStepKey;
        const statusLabel = step.status === "confirmed"
          ? "已完成"
          : isCurrent && step.status === "stale"
            ? "现在重新检查"
            : isCurrent && step.status === "blocked"
              ? "现在处理"
              : isCurrent
                ? "可以确认"
                : canWorkAhead
                  ? "可先做"
                  : step.status === "stale"
                    ? "稍后重新检查"
                    : "稍后处理";
        return (
          <button
            type="button"
            className="monthly-close-step"
            key={step.step_key}
            aria-current={selected ? "step" : undefined}
            onClick={() => onSelect?.(step.step_key)}
            style={{
              appearance: "none",
              width: "100%",
              display: "grid",
              gridTemplateColumns: "32px minmax(0, 1fr)",
              gap: 12,
              padding: "14px 12px",
              borderTop: 0,
              borderRight: 0,
              borderLeft: selected ? `2px solid ${tokens.anyu.color.ink.default}` : "2px solid transparent",
              borderBottom: `0.5px solid ${tokens.anyu.color.linen}`,
              background: selected ? tokens.anyu.color.sand : "transparent",
              color: "inherit",
              cursor: "pointer",
              font: "inherit",
              textAlign: "left",
            }}
          >
            <span
              className="serif"
              style={{
                width: 28,
                height: 28,
                borderRadius: 999,
                border: `1px solid ${statusTone[step.status]}`,
                color: statusTone[step.status],
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              {step.status === "confirmed"
                ? <CheckOutlined />
                : step.status === "locked" && !canWorkAhead
                  ? <LockOutlined />
                  : step.position}
            </span>
            <span className="monthly-close-step-copy" style={{ minWidth: 0 }}>
              <span style={{ display: "block", color: tokens.color.text.primary }}>{step.label}</span>
              <span style={{ display: "block", marginTop: 2, color: statusTone[step.status], fontSize: 12 }}>
                {statusLabel}
                {step.blocking_count > 0 ? ` · ${step.blocking_count} 项` : ""}
              </span>
            </span>
          </button>
        );
      })}
    </nav>
  );
}
