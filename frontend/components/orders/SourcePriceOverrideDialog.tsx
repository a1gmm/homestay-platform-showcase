"use client";

import { useEffect, useMemo, useState } from "react";
import { Button, Input, Modal } from "antd";
import dayjs from "dayjs";

import { useManualOverrides } from "@/hooks/useManualOverrides";
import type { SourcePriceSnapshot } from "@/lib/types";

interface SourcePriceOverrideDialogProps {
  open: boolean;
  orderId: string;
  canAdminister: boolean;
  checkInDate: string;
  checkOutDate: string;
  currentSnapshot: SourcePriceSnapshot | null;
  onClose: () => void;
  onSaved: (snapshot: SourcePriceSnapshot) => void;
}

function money(value: string | null | undefined): string {
  if (!value) return "—";
  return `¥${Number(value).toFixed(2)}`;
}

export function SourcePriceOverrideDialog({
  open,
  orderId,
  canAdminister,
  checkInDate,
  checkOutDate,
  currentSnapshot,
  onClose,
  onSaved,
}: SourcePriceOverrideDialogProps) {
  const { sourcePriceMutation } = useManualOverrides(orderId);
  const [mode, setMode] = useState<"total" | "nightly">("total");
  const [total, setTotal] = useState("");
  const [nightlyPrices, setNightlyPrices] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setMode("total");
    setTotal("");
    const seededNightly: Record<string, string> = {};
    for (
      let cursor = dayjs(checkInDate);
      cursor.isBefore(dayjs(checkOutDate), "day");
      cursor = cursor.add(1, "day")
    ) {
      const stayDate = cursor.format("YYYY-MM-DD");
      seededNightly[stayDate] = String(currentSnapshot?.nightly_bases?.[stayDate] ?? "");
    }
    setNightlyPrices(seededNightly);
    setReason("");
    setStatus("");
    setError("");
  }, [open, currentSnapshot?.source_price_snapshot_id, checkInDate, checkOutDate]);

  const parsedTotal = Number(total);
  const totalIsValid = total.trim() !== "" && Number.isFinite(parsedTotal) && parsedTotal >= 0;
  const nightlyEntries = Object.entries(nightlyPrices).sort(([left], [right]) =>
    left.localeCompare(right),
  );
  const nightlyIsValid =
    nightlyEntries.length > 0 &&
    nightlyEntries.every(([, value]) => {
      const amount = Number(value);
      return value.trim() !== "" && Number.isFinite(amount) && amount >= 0;
    });
  const selectedPriceIsValid = mode === "total" ? totalIsValid : nightlyIsValid;
  const canSubmit = selectedPriceIsValid && reason.trim().length >= 2;
  const nightlyTotal = nightlyEntries.reduce((sum, [, value]) => sum + Number(value || 0), 0);
  const correctedAmount = useMemo(
    () =>
      selectedPriceIsValid
        ? money((mode === "total" ? parsedTotal : nightlyTotal).toFixed(2))
        : "—",
    [mode, nightlyTotal, parsedTotal, selectedPriceIsValid],
  );

  if (!canAdminister) return null;

  const submit = async () => {
    if (!canSubmit) return;
    setError("");
    try {
      const snapshot = await sourcePriceMutation.mutateAsync({
        ...(currentSnapshot
          ? { based_on_snapshot_id: currentSnapshot.source_price_snapshot_id }
          : {}),
        ...(mode === "total"
          ? { total: parsedTotal.toFixed(2) }
          : {
              nightly_prices: Object.fromEntries(
                nightlyEntries.map(([date, value]) => [date, Number(value).toFixed(2)]),
              ),
            }),
        reason: reason.trim(),
      });
      setStatus(`已创建新来源价格快照 ${snapshot.source_price_snapshot_id}`);
      onSaved(snapshot);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "来源价格更正失败，请稍后重试");
    }
  };

  return (
    <Modal
      open={open}
      title="更正来源价格"
      onCancel={() => !sourcePriceMutation.isPending && onClose()}
      footer={
        <div className="manual-modal-footer">
          <Button disabled={sourcePriceMutation.isPending} onClick={onClose}>
            取消
          </Button>
          <Button
            type="primary"
            loading={sourcePriceMutation.isPending}
            disabled={!canSubmit}
            onClick={submit}
          >
            保存来源价格更正
          </Button>
        </div>
      }
      destroyOnHidden
    >
      <div className="source-price-dialog">
        <p>当前不可变快照</p>
        <p>原快照保持不变；保存后将追加一条带原因的新来源价格快照。</p>
        <dl className="source-price-comparison">
          <div>
            <dt>当前来源总价</dt>
            <dd>{money(currentSnapshot?.total)}</dd>
          </div>
          <div>
            <dt>更正后</dt>
            <dd>更正后 {correctedAmount}</dd>
          </div>
        </dl>
        <fieldset className="source-price-mode">
          <legend>更正方式</legend>
          <label>
            <input
              type="radio"
              name="source-price-mode"
              checked={mode === "total"}
              onChange={() => setMode("total")}
            />
            总价
          </label>
          <label>
            <input
              type="radio"
              name="source-price-mode"
              checked={mode === "nightly"}
              disabled={nightlyEntries.length === 0}
              onChange={() => setMode("nightly")}
            />
            逐夜价格
          </label>
        </fieldset>
        {mode === "total" ? (
          <label className="manual-reason-field">
            <span>新来源总价</span>
            <Input
              aria-label="新来源总价"
              inputMode="decimal"
              value={total}
              onChange={(event) => setTotal(event.target.value)}
            />
          </label>
        ) : (
          <div className="source-price-nightly">
            {nightlyEntries.map(([date, value]) => (
              <label key={date} className="manual-reason-field">
                <span>{date}</span>
                <Input
                  aria-label={`${date} 来源价格`}
                  inputMode="decimal"
                  value={value}
                  onChange={(event) =>
                    setNightlyPrices((current) => ({
                      ...current,
                      [date]: event.target.value,
                    }))
                  }
                />
              </label>
            ))}
          </div>
        )}
        <label className="manual-reason-field">
          <span>更正原因</span>
          <Input.TextArea
            aria-label="更正原因"
            value={reason}
            maxLength={200}
            onChange={(event) => setReason(event.target.value)}
            autoSize={{ minRows: 3, maxRows: 5 }}
          />
        </label>
        <div aria-live="polite" role="status">
          {status}
        </div>
        {error ? <div role="alert">{error}</div> : null}
      </div>
    </Modal>
  );
}

export default SourcePriceOverrideDialog;
