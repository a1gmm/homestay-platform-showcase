"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Modal } from "antd";
import dayjs from "dayjs";

import { useSplitStay } from "@/hooks/useSplitStay";
import { CHANNEL_LABELS } from "@/lib/channels";
import type {
  OrderManualControl,
  OrderOut,
  RoomOut,
  SplitStaySegmentDraft,
  StaySettlementKind,
  ZeroFeeSplitPayload,
  ZeroFeeSplitResult,
} from "@/lib/types";
import { StaySettlementLabel } from "@/components/ui/StaySettlementLabel";

interface SplitStayDialogProps {
  open: boolean;
  embedded?: boolean;
  order: OrderOut;
  control: OrderManualControl;
  rooms: RoomOut[];
  autoPreviewKey?: number;
  onBack: () => void;
  onComplete: (result: ZeroFeeSplitResult) => void;
  onPendingChange?: (pending: boolean) => void;
}

function dateNights(checkIn: string, checkOut: string): string[] {
  const nights: string[] = [];
  for (let cursor = dayjs(checkIn); cursor.isBefore(dayjs(checkOut), "day"); cursor = cursor.add(1, "day")) {
    nights.push(cursor.format("YYYY-MM-DD"));
  }
  return nights;
}

export function validateSplitCoverage(
  checkIn: string,
  checkOut: string,
  segments: SplitStaySegmentDraft[],
): string | null {
  const expectedNights = dateNights(checkIn, checkOut);
  const covered = new Set<string>();
  for (const segment of segments) {
    for (const night of dateNights(segment.check_in_date, segment.check_out_date)) {
      if (covered.has(night)) return "住宿日期存在重叠，请检查后重新预览";
      covered.add(night);
    }
  }
  if (
    covered.size !== expectedNights.length ||
    expectedNights.some((night) => !covered.has(night))
  ) {
    return "住宿日期未完整覆盖，请检查后重新预览";
  }
  return null;
}

export function mergeAdjacentDrafts(
  segments: SplitStaySegmentDraft[],
): SplitStaySegmentDraft[] {
  return [...segments]
    .sort((left, right) => left.check_in_date.localeCompare(right.check_in_date))
    .reduce<SplitStaySegmentDraft[]>((merged, segment) => {
      const previous = merged.at(-1);
      if (
        previous &&
        previous.check_out_date === segment.check_in_date &&
        previous.room_id === segment.room_id &&
        previous.settlement_kind === segment.settlement_kind
      ) {
        previous.check_out_date = segment.check_out_date;
        return merged;
      }
      return [...merged, { ...segment }];
    }, []);
}

function money(value: string): string {
  return `¥${Number(value).toFixed(2)}`;
}

function currentRoomId(order: OrderOut): string {
  const rooms = (order as OrderOut & { rooms?: Array<{ room_id?: string }> }).rooms;
  return rooms?.[0]?.room_id || order.room_id || "";
}

export function SplitStayDialog({
  open,
  embedded = false,
  order,
  control,
  rooms,
  autoPreviewKey,
  onBack,
  onComplete,
  onPendingChange,
}: SplitStayDialogProps) {
  const split = useSplitStay(control.source_order_id);
  const nights = useMemo(
    () => dateNights(order.check_in_date, order.check_out_date),
    [order.check_in_date, order.check_out_date],
  );
  const [drafts, setDrafts] = useState<SplitStaySegmentDraft[]>(() =>
    nights.map((night) => ({
      check_in_date: night,
      check_out_date: dayjs(night).add(1, "day").format("YYYY-MM-DD"),
      room_id: currentRoomId(order),
      settlement_kind: "free_room",
    })),
  );
  const [announcement, setAnnouncement] = useState("");
  const [localError, setLocalError] = useState("");
  const lastAutoPreviewKey = useRef<number | undefined>(undefined);
  const snapshot = control.source_price_snapshot;

  useEffect(() => {
    onPendingChange?.(split.isPending);
  }, [onPendingChange, split.isPending]);

  const payload = (expectedVersion: number): ZeroFeeSplitPayload => ({
    expected_group_version: expectedVersion,
    price_snapshot_id: snapshot?.source_price_snapshot_id || "",
    segments: mergeAdjacentDrafts(drafts),
  });

  useEffect(() => {
    if (
      !open ||
      autoPreviewKey === undefined ||
      lastAutoPreviewKey.current === autoPreviewKey
    ) {
      return;
    }
    lastAutoPreviewKey.current = autoPreviewKey;
    const coverageError = validateSplitCoverage(
      order.check_in_date,
      order.check_out_date,
      drafts,
    );
    if (!control.split.eligible || !snapshot || coverageError) {
      setLocalError(
        coverageError || control.split.blocker_message || "缺少可追溯的来源价格，无法拆分",
      );
      return;
    }
    setLocalError("");
    setAnnouncement("");
    void split
      .requestPreview(payload(control.split.group_version))
      .then((result) => result && setAnnouncement("服务器预览已更新"));
    // The numeric key is the explicit one-shot trigger. Draft/control changes alone
    // must never silently replace a preview after an operator edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoPreviewKey, open]);

  if (!open) return null;

  const updateDraft = (
    index: number,
    field: "room_id" | "settlement_kind",
    value: string,
  ) => {
    const hadPreview =
      split.phase === "previewing" ||
      split.phase === "preview_valid" ||
      Boolean(split.preview);
    setDrafts((current) =>
      current.map((draft, draftIndex) =>
        draftIndex === index
          ? { ...draft, [field]: value as StaySettlementKind }
          : draft,
      ),
    );
    split.invalidatePreview();
    setLocalError("");
    if (hadPreview) setAnnouncement("内容已修改，请重新预览");
  };

  const previewDraft = async () => {
    const coverageError = validateSplitCoverage(
      order.check_in_date,
      order.check_out_date,
      drafts,
    );
    if (coverageError) {
      setLocalError(coverageError);
      return;
    }
    if (!snapshot) {
      setLocalError("缺少可追溯的来源价格，无法拆分");
      return;
    }
    setLocalError("");
    setAnnouncement("");
    const result = await split.requestPreview(payload(control.split.group_version));
    if (result) setAnnouncement("服务器预览已更新");
  };

  const submit = async () => {
    if (!split.preview) return;
    const result = await split.submit();
    if (result) onComplete(result);
  };

  const content = split.receipt ? (
    <div className="split-stay-receipt">
      <h2>拆分完成</h2>
      <p>客人应付 ¥0</p>
      <dl>
        {split.receipt.segments.map((segment, index) => (
          <div key={`${segment.check_in_date}-${segment.order_id || index}`}>
            <dt>
              {segment.check_in_date} — {segment.check_out_date}
            </dt>
            <dd>
              <span>{segment.order_id || "—"}</span> · {segment.room_id} ·{" "}
              <StaySettlementLabel kind={segment.settlement_kind} />
            </dd>
          </div>
        ))}
      </dl>
      <Button type="primary" onClick={onBack}>
        返回订单详情
      </Button>
    </div>
  ) : (
    <div className="split-stay-dialog">
      <div className="split-stay-heading">
        <div>
          <h2>拆分住宿段</h2>
          <p>仅拆分住宿记录，不产生客人房费。金额以服务器预览为准。</p>
        </div>
        {snapshot ? <span>来源价格 {money(snapshot.total)}</span> : null}
      </div>

      <div className="split-night-list">
        {drafts.map((draft, index) => (
          <fieldset
            key={draft.check_in_date}
            aria-label={`第 ${index + 1} 晚 · ${draft.check_in_date}`}
            className="split-night-row"
          >
            <legend>第 {index + 1} 晚 · {draft.check_in_date}</legend>
            <label>
              <span>房间</span>
              <select
                aria-label={`第 ${index + 1} 晚房间`}
                value={draft.room_id}
                onChange={(event) => updateDraft(index, "room_id", event.target.value)}
              >
                {rooms.map((room) => (
                  <option key={room.room_id} value={room.room_id}>
                    {room.room_name || room.room_id}
                  </option>
                ))}
              </select>
            </label>
            <div className="split-night-kind">
              <label>
                <input
                  type="radio"
                  name={`settlement-${index}`}
                  checked={draft.settlement_kind === "free_room"}
                  onChange={() => updateDraft(index, "settlement_kind", "free_room")}
                />
                免房
              </label>
              <label>
                <input
                  type="radio"
                  name={`settlement-${index}`}
                  checked={draft.settlement_kind === "company_sponsored"}
                  onChange={() => updateDraft(index, "settlement_kind", "company_sponsored")}
                />
                公司承担
              </label>
            </div>
          </fieldset>
        ))}
      </div>

      {split.preview ? (
        <section aria-labelledby="split-preview-heading" className="split-preview">
          <h3 id="split-preview-heading">结算影响</h3>
          {split.preview.segments.map((segment) => {
            const finance = segment.company_sponsored;
            if (!finance) return null;
            return (
              <p key={segment.check_in_date}>
                {CHANNEL_LABELS[order.channel] || order.channel} {money(finance.calculation_base)} ×{" "}
                {Number(finance.settlement_ratio).toFixed(2)} = {money(finance.amount)}
              </p>
            );
          })}
          <p>将锁定：日期、房间、住宿结构、金额</p>
        </section>
      ) : null}

      <div role="status" aria-live="polite">
        {announcement}
      </div>
      {localError || split.error ? (
        <div role="alert">{localError || split.error}</div>
      ) : null}

      <div className="split-stay-footer">
        <Button disabled={split.isPending} onClick={onBack}>
          返回
        </Button>
        <Button loading={split.phase === "previewing"} disabled={split.isPending} onClick={previewDraft}>
          预览拆分
        </Button>
        <Button
          type="primary"
          loading={split.phase === "submitting"}
          disabled={split.phase !== "preview_valid"}
          onClick={submit}
        >
          确认拆分
        </Button>
      </div>
    </div>
  );

  if (embedded) return content;
  return (
    <Modal open={open} footer={null} onCancel={() => !split.isPending && onBack()} destroyOnHidden>
      {content}
    </Modal>
  );
}

export default SplitStayDialog;
