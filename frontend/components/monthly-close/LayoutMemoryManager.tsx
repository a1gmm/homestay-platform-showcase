import { useState } from "react";
import { Alert, Button, Empty, Tag } from "antd";

import type { MonthlyCloseLayoutMemory, MonthlyCloseLayoutMetrics } from "@/lib/monthly-close";
import { MONTHLY_CLOSE_SOURCE_LABELS } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";
import { extractErrorMessage } from "@/lib/api-errors";

export function LayoutMemoryManager({ metrics, memories, onSetEnabled }: {
  metrics: MonthlyCloseLayoutMetrics;
  memories: MonthlyCloseLayoutMemory[];
  onSetEnabled: (documentId: string, enabled: boolean) => Promise<unknown>;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const toggle = async (documentId: string, enabled: boolean) => {
    setBusyId(documentId);
    setLocalError(null);
    try {
      await onSetEnabled(documentId, enabled);
    } catch (error) {
      setLocalError(extractErrorMessage(error, "布局记忆更新失败，请重试"));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 12 }}>
        {[["自动化率", `${Math.round(metrics.automation_rate)}%`], ["文件总数", String(metrics.file_count)], ["确定性识别", String(metrics.deterministic_recognition_count)], ["AI 建议", String(metrics.ai_suggestion_count)], ["沿用记忆", String(metrics.remembered_layout_count)], ["管理员修正", String(metrics.administrator_correction_count)], ["需要人工确认", String(metrics.needs_confirmation_count)], ["有效布局", String(metrics.active_memory_count)], ["累计自动沿用", String(metrics.reuse_count)]].map(([label, value]) => (
          <div key={label}><div style={{ color: tokens.color.text.tertiary, fontSize: 12 }}>{label}</div><div className="serif" style={{ fontSize: 26, marginTop: 3 }}>{value}</div></div>
        ))}
      </div>
      <div style={{ color: tokens.color.text.secondary }}>同一个供应商以后换列名或列顺序，管理员确认一次后系统会自动沿用；错的记忆可以随时停用。</div>
      {localError && <Alert type="error" showIcon message={localError} />}
      {memories.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有已确认的布局记忆" /> : memories.map((memory) => (
        <div key={memory.document_id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, borderTop: `0.5px solid ${tokens.anyu.color.linen}`, paddingTop: 12 }}>
          <div style={{ minWidth: 0 }}>
            <div style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{memory.filename}</div>
            <div style={{ color: tokens.color.text.tertiary, fontSize: 12, marginTop: 3 }}>
              <span>{MONTHLY_CLOSE_SOURCE_LABELS[memory.source_type] || memory.source_type}</span> · <span>{`已自动沿用 ${memory.use_count} 次`}</span>
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Tag color={memory.enabled ? "success" : "default"}>{memory.enabled ? "使用中" : "已停用"}</Tag>
            <Button loading={busyId === memory.document_id} disabled={busyId !== null} aria-label={`${memory.enabled ? "停用" : "恢复"}${memory.filename}布局记忆`} onClick={() => void toggle(memory.document_id, !memory.enabled)}>{memory.enabled ? "停用" : "恢复"}</Button>
          </div>
        </div>
      ))}
    </div>
  );
}
