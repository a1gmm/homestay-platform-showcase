import React from "react";

import { tokens } from "@/lib/design-tokens";
import type { StaySettlementKind } from "@/lib/types";

export type StaySettlementLabelKind = StaySettlementKind | "manual_override";

const COPY: Record<
  StaySettlementLabelKind,
  { full: string; compact: string; color: string }
> = {
  free_room: { full: "免房", compact: "免房", color: tokens.anyu.color.stone },
  company_sponsored: {
    full: "公司承担",
    compact: "企付",
    color: tokens.anyu.color.clay,
  },
  manual_override: {
    full: "人工接管",
    compact: "人工",
    color: tokens.anyu.color.clay,
  },
};

export function StaySettlementLabel({
  kind,
  compact = false,
}: {
  kind?: StaySettlementLabelKind | null;
  compact?: boolean;
}) {
  if (!kind) return null;
  const copy = COPY[kind];
  return (
    <span
      className="status-dot"
      aria-label={copy.full}
      style={{
        color: copy.color,
        fontWeight: 400,
        letterSpacing: 0,
        whiteSpace: "nowrap",
      }}
    >
      {compact ? copy.compact : copy.full}
    </span>
  );
}
