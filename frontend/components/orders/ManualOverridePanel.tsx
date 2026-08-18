"use client";

import { useMemo, useState } from "react";
import { Button, Input, Modal } from "antd";

import { useManualOverrides } from "@/hooks/useManualOverrides";
import type {
  ManualOverrideField,
  OrderManualControl,
  OrderSyncConflict,
} from "@/lib/types";

const FIELD_LABELS: Record<ManualOverrideField, string> = {
  guest_name: "客人姓名",
  guest_profile: "客人资料",
  check_in_date: "入住日期",
  check_out_date: "离店日期",
  room_assignment: "房间",
  stay_structure: "住宿结构",
  actual_price: "实收金额",
  daily_prices: "每日价格",
  ota_owner_revenue: "房东收入",
  channel: "渠道",
  note: "备注",
  order_status: "订单状态",
};

type DecisionAction = "preserve" | "ignore" | "restore";

interface ManualOverridePanelProps {
  orderId: string;
  control: OrderManualControl;
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.join("、");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function ManualOverridePanel({ orderId, control }: ManualOverridePanelProps) {
  const { unlockMutation, conflictMutation } = useManualOverrides(orderId);
  const [dialog, setDialog] = useState<{
    action: DecisionAction;
    conflict: OrderSyncConflict;
  } | null>(null);
  const [reason, setReason] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  const isPending = unlockMutation.isPending || conflictMutation.isPending;
  const dialogTitle = useMemo(() => {
    if (!dialog) return "";
    if (dialog.action === "restore") return `恢复跟随宝寓 · ${FIELD_LABELS[dialog.conflict.field]}`;
    if (dialog.action === "ignore") return `忽略本次差异 · ${FIELD_LABELS[dialog.conflict.field]}`;
    return `继续人工接管 · ${FIELD_LABELS[dialog.conflict.field]}`;
  }, [dialog]);

  const openDialog = (action: DecisionAction, conflict: OrderSyncConflict) => {
    setDialog({ action, conflict });
    setReason("");
    setError("");
  };

  const confirm = async () => {
    if (!dialog || reason.trim().length < 2) return;
    setError("");
    try {
      if (dialog.action === "restore") {
        await unlockMutation.mutateAsync({
          action: "unlock",
          fields: [dialog.conflict.field],
          reason: reason.trim(),
        });
        setStatus("已恢复跟随宝寓，等待下次同步");
      } else {
        await conflictMutation.mutateAsync({
          conflictId: dialog.conflict.conflict_id,
          action: dialog.action,
          reason: reason.trim(),
        });
        setStatus(
          dialog.action === "preserve"
            ? "已记录继续接管；宝寓差异仍保留"
            : "已忽略本次宝寓差异；来源版本变化后将重新提示",
        );
      }
      setDialog(null);
      setReason("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请稍后重试");
    }
  };

  return (
    <div className="manual-control-panel">
      <section aria-labelledby="sync-conflicts-heading" className="manual-control-section">
        <div className="manual-control-heading-row">
          <div>
            <h3 id="sync-conflicts-heading">待处理宝寓差异</h3>
            <p>先确认差异影响，再决定继续人工接管或恢复跟随宝寓。</p>
          </div>
          <span aria-label={`${control.open_conflicts.length} 项待处理`}>
            {control.open_conflicts.length} 项待处理
          </span>
        </div>

        {control.open_conflicts.length === 0 ? (
          <p className="manual-control-empty">当前没有待处理差异。</p>
        ) : (
          <div className="manual-conflict-list">
            {control.open_conflicts.map((conflict) => (
              <article key={conflict.conflict_id} className="manual-conflict-row">
                <div className="manual-conflict-copy">
                  <h4>{FIELD_LABELS[conflict.field]}</h4>
                  <dl>
                    <div>
                      <dt>PMS 当前</dt>
                      <dd>{displayValue(conflict.local_value)}</dd>
                    </div>
                    <div>
                      <dt>宝寓最新</dt>
                      <dd>{displayValue(conflict.upstream_value)}</dd>
                    </div>
                  </dl>
                </div>
                <div className="manual-conflict-actions">
                  <Button
                    disabled={!control.can_write}
                    onClick={() => openDialog("preserve", conflict)}
                  >
                    保留 PMS 并继续接管
                  </Button>
                  {control.can_administer ? (
                    <>
                      <Button
                        type="primary"
                        disabled={!conflict.can_restore_following}
                        onClick={() => openDialog("restore", conflict)}
                      >
                        恢复跟随宝寓
                      </Button>
                      {!conflict.can_restore_following ? (
                        <span className="manual-control-contact">托管拆分结构请走更正流程</span>
                      ) : null}
                      <button
                        type="button"
                        className="manual-text-action"
                        onClick={() => openDialog("ignore", conflict)}
                      >
                        忽略
                      </button>
                    </>
                  ) : (
                    <span className="manual-control-contact">联系管理员恢复跟随宝寓</span>
                  )}
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section aria-labelledby="manual-ownership-heading" className="manual-control-section">
        <div className="manual-control-heading-row">
          <div>
            <h3 id="manual-ownership-heading">人工接管</h3>
            <p>这些字段继续以 PMS 当前值为准，宝寓同步不会覆盖。</p>
          </div>
        </div>
        {control.locked_fields.length === 0 ? (
          <p className="manual-control-empty">当前没有人工接管字段。</p>
        ) : (
          <dl className="manual-locked-fields">
            {control.locked_fields.map((field) => (
              <div key={field.field}>
                <dt>{FIELD_LABELS[field.field]}</dt>
                <dd>{displayValue(field.current_value)}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>

      <div className="manual-control-live" aria-live="polite" role="status">
        {status}
      </div>
      {error ? <div role="alert">{error}</div> : null}

      <Modal
        open={Boolean(dialog)}
        title={dialogTitle}
        onCancel={() => !isPending && setDialog(null)}
        footer={
          <div className="manual-modal-footer">
            <Button disabled={isPending} onClick={() => setDialog(null)}>
              取消
            </Button>
            <Button
              type="primary"
              loading={isPending}
              disabled={reason.trim().length < 2}
              onClick={confirm}
            >
              {dialog?.action === "restore"
                ? "确认恢复跟随"
                : dialog?.action === "preserve"
                  ? "确认保留 PMS"
                  : "确认忽略"}
            </Button>
          </div>
        }
        destroyOnHidden
      >
        {dialog ? (
          <div className="manual-dialog-body">
            <dl>
              <div>
                <dt>PMS 当前</dt>
                <dd>{displayValue(dialog.conflict.local_value)}</dd>
              </div>
              <div>
                <dt>宝寓最新</dt>
                <dd>{displayValue(dialog.conflict.upstream_value)}</dd>
              </div>
            </dl>
            {dialog.action === "restore" ? (
              <p>
                当前值不会立即变化；下一次同步后可能更新为「
                {displayValue(dialog.conflict.upstream_value)}」
              </p>
            ) : dialog.action === "preserve" ? (
              <p>宝寓差异会继续保留，后续仍可由管理员恢复跟随。</p>
            ) : (
              <p>本次差异会被忽略；宝寓来源版本变化后会再次提示。</p>
            )}
            <label className="manual-reason-field">
              <span>{dialog.action === "restore" ? "恢复原因" : "处理原因"}</span>
              <Input.TextArea
                aria-label={dialog.action === "restore" ? "恢复原因" : "处理原因"}
                value={reason}
                maxLength={200}
                onChange={(event) => setReason(event.target.value)}
                autoSize={{ minRows: 3, maxRows: 5 }}
              />
            </label>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}

export default ManualOverridePanel;
