"use client";

import { useState } from "react";
import { Alert, Button, Input, Modal, Space, Typography } from "antd";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { tasksApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import { usePrivacyMode } from "@/hooks/usePrivacyMode";
import { readPrivacyMode, PrivacyModeReadOnlyError } from "@/lib/privacy-mode";
import type { TaskOut } from "@/lib/types";

export function HistoricalTaskArchiveModal({ task, onClose }: { task: TaskOut; onClose: () => void }) {
  const qc = useQueryClient();
  const privacyMode = usePrivacyMode();
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<{ fingerprint: string; effect: string } | null>(null);
  const mutation = useMutation({
    mutationFn: async (apply: boolean) => {
      if (readPrivacyMode()) throw new PrivacyModeReadOnlyError();
      return tasksApi.archive(task.task_id, apply ? { apply: true, reason: reason.trim(), expected_fingerprint: preview?.fingerprint } : {});
    },
    onSuccess: (response, apply) => {
      if (apply) {
        qc.invalidateQueries({ queryKey: ["tasks"] });
        qc.invalidateQueries({ queryKey: ["dashboard"] });
        onClose();
      } else setPreview(response.data);
    },
    onError: () => setPreview(null),
  });
  return <Modal open title="核实并归档历史任务" onCancel={onClose} footer={null}>
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Typography.Text>{task.title} · {task.room_id || "未关联房间"} · {task.order_id}</Typography.Text>
      <Alert type="warning" showIcon message="请先核实这项旧待办已无需继续执行"
        description="创建满 7 天、关联订单已结束只代表需要核查。若仍需保洁，请返回安排任务；归档会保留原记录和原因，不等同于完工或查房通过。" />
      <Input.TextArea aria-label="归档核实原因" rows={3} maxLength={1000} value={reason}
        disabled={privacyMode || mutation.isPending} placeholder="填写核实依据及无需继续执行的原因"
        onChange={e => { setReason(e.target.value); setPreview(null); }} />
      {preview && <Alert type="info" message="归档影响预览" description={preview.effect} showIcon />}
      {mutation.isError && <Alert type="error" showIcon message="归档未完成"
        description={extractErrorMessage(mutation.error, "请重试预览并核实最新状态")} />}
      <Space wrap>
        <Button style={{ minHeight: 44 }} onClick={onClose}>保留任务</Button>
        <Button style={{ minHeight: 44 }} disabled={privacyMode || !reason.trim() || mutation.isPending} onClick={() => mutation.mutate(false)}>预览归档影响</Button>
        <Button style={{ minHeight: 44 }} type="primary" disabled={privacyMode || !preview || !reason.trim()} loading={mutation.isPending}
          onClick={() => mutation.mutate(true)}>确认归档此任务</Button>
      </Space>
    </Space>
  </Modal>;
}
