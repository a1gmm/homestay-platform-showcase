import type { StayGroup, StaySettlementKind } from "@/lib/types";
import { StaySettlementLabel } from "@/components/ui/StaySettlementLabel";

export function hasStaySettlementLabels(group: Pick<StayGroup, "segments">): boolean {
  return group.segments.some((segment) => Boolean(segment.stay_settlement_kind));
}

export function StayGroupSettlementLabels({
  group,
  compact = false,
}: {
  group: Pick<StayGroup, "segments">;
  compact?: boolean;
}) {
  const kinds = Array.from(
    new Set(
      group.segments
        .map((segment) => segment.stay_settlement_kind)
        .filter((kind): kind is StaySettlementKind => Boolean(kind)),
    ),
  );
  const hasManualOwnership = group.segments.some(
    (segment) =>
      segment.is_manually_managed || (segment.manual_override_fields?.length ?? 0) > 0,
  );
  if (kinds.length === 0 && !hasManualOwnership) return null;
  return (
    <span className="stay-settlement-labels">
      {kinds.map((kind) => (
        <StaySettlementLabel key={kind} kind={kind} compact={compact} />
      ))}
      {hasManualOwnership ? (
        <StaySettlementLabel kind="manual_override" compact={compact} />
      ) : null}
    </span>
  );
}
