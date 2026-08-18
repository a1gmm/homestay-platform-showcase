import { Tag } from "antd";
import { tokens } from "@/lib/design-tokens";

export type FreeRoomKind = "none" | "all" | "mixed";

export function freeRoomLabel(kind?: FreeRoomKind | null, compact = false): string | null {
  if (kind === "all") return compact ? "免房" : "免房订单";
  if (kind === "mixed") return compact ? "含免房" : "含免房订单";
  return null;
}

export function FreeRoomBadge({ kind, compact = false }: { kind?: FreeRoomKind | null; compact?: boolean }) {
  const label = freeRoomLabel(kind, compact);
  if (!label) return null;
  return (
    <Tag
      aria-label={label}
      style={{
        marginInlineEnd: 0,
        color: tokens.anyu.color.clay,
        borderColor: tokens.anyu.color.clay,
        background: "transparent",
        borderRadius: tokens.radius.full,
        fontSize: compact ? 9 : 11,
        lineHeight: compact ? "16px" : "20px",
        paddingInline: compact ? 4 : 7,
        fontWeight: 500,
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </Tag>
  );
}
