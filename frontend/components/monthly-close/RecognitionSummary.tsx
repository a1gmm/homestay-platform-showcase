import { useState, type ReactNode } from "react";

import { tokens } from "@/lib/design-tokens";

export interface RecognitionFact {
  label: string;
  value: ReactNode;
}

export function RecognitionSummary({
  documentName,
  sourceLabel,
  facts,
  note,
  warning,
  isAdmin,
  advanced,
  onAdvancedChange,
}: {
  documentName: string;
  sourceLabel: string;
  facts: RecognitionFact[];
  note?: ReactNode;
  warning?: ReactNode;
  isAdmin: boolean;
  advanced: ReactNode;
  onAdvancedChange?: (open: boolean) => void;
}) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const toggleAdvanced = () => {
    const next = !advancedOpen;
    setAdvancedOpen(next);
    onAdvancedChange?.(next);
  };

  return (
    <section aria-label={`${documentName} 识别摘要`} style={{ display: "grid", gap: 16 }}>
      <div style={{ display: "grid", gap: 6 }}>
        <h3 style={{ margin: 0, color: tokens.anyu.color.ink.default, fontSize: 18, fontWeight: 500 }}>
          我把它识别为{sourceLabel}
        </h3>
        <div style={{ color: tokens.anyu.color.stone, overflowWrap: "anywhere" }}>{documentName}</div>
      </div>

      {warning && (
        <div role="alert" style={{ borderLeft: `3px solid ${tokens.anyu.color.clay}`, padding: "9px 12px", background: tokens.anyu.color.sand, color: tokens.anyu.color.ink.default, lineHeight: 1.65 }}>
          {warning}
        </div>
      )}

      {facts.length > 0 && (
        <dl style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(132px, 1fr))", gap: 8, margin: 0 }}>
          {facts.map((fact) => (
            <div key={fact.label} style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 8, padding: "10px 12px", background: tokens.anyu.color.shell }}>
              <dt style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>{fact.label}</dt>
              <dd style={{ margin: "4px 0 0", color: tokens.anyu.color.ink.default, fontSize: 16, fontWeight: 500, overflowWrap: "anywhere" }}>{fact.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {note && <div style={{ color: tokens.anyu.color.stone, lineHeight: 1.7 }}>{note}</div>}

      <div style={{ borderTop: `0.5px solid ${tokens.anyu.color.linen}`, paddingTop: 12 }}>
        <button
          type="button"
          aria-expanded={advancedOpen}
          onClick={toggleAdvanced}
          style={{ minHeight: 44, border: 0, padding: "0 4px", background: "transparent", color: tokens.anyu.color.ink.default, font: "inherit", textDecoration: "underline", textUnderlineOffset: 4 }}
        >
          {advancedOpen ? "收起读取设置" : "识别不对，调整读取方式"}
        </button>
      </div>

      {advancedOpen && (
        isAdmin ? (
          <section aria-label="管理员读取设置" style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 8, padding: 14, background: tokens.anyu.color.sand }}>
            <div style={{ marginBottom: 12, color: tokens.anyu.color.stone, fontSize: 12 }}>管理员读取设置</div>
            {advanced}
          </section>
        ) : (
          <div role="status" style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 8, padding: 12, background: tokens.anyu.color.sand, lineHeight: 1.65 }}>
            请联系管理员调整表格读取方式。原文件和当前识别结果都不会丢失。
          </div>
        )
      )}
    </section>
  );
}
