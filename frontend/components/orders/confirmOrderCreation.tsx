"use client";

import React from "react";
import { Button, Modal, Space } from "antd";
import { extractErrorMessage, isDuplicateOrderError } from "@/lib/api-errors";
import type { ResolveCreationDecision } from "@/lib/create-orders";
import type { OrderCreate } from "@/lib/types";

interface Candidate {
  order_id: string;
  guest_name: string;
  rooms: { room_id: string; check_in_date: string; check_out_date: string }[];
}

/** One decision per original stay within this batch, including split-room entry. */
export function createOrderDecisionResolver(): ResolveCreationDecision {
  const linked = new Set<string>();
  return async (error, payload) => {
    const response = (error as { response?: { status?: number; data?: { detail?: {
      code?: string; fingerprint?: string; candidates?: Candidate[];
    } } } })?.response;
    const detail = response?.data?.detail;
    if (response?.status === 409 && detail?.code === "continuation_decision_required"
      && detail.fingerprint && detail.candidates?.length) {
      const { candidates, fingerprint } = detail;
      const candidate = candidates[0];
      const withDecision = (action: "link" | "separate"): OrderCreate => ({
        ...payload, continuation: { action, fingerprint,
          order_id: action === "link" ? candidate.order_id : null },
      });
      if (candidates.length === 1 && linked.has(candidate.order_id)) return withDecision("link");
      return new Promise((resolve) => {
        const modal = Modal.confirm({
          title: "这次是同一位客人续住吗？",
          content: <Space direction="vertical" style={{ width: "100%" }}>
            <div>找到同房、日期相接的订单。确认续住后会一并创建和关联，不用再单独操作。</div>
            {candidates.map((c) => <div key={c.order_id}>
              <div>{c.guest_name} · {c.order_id}</div>
              {c.rooms.map((r, index) => <div key={`${r.room_id}-${index}`}>
                {r.room_id} 房 · {r.check_in_date} 至 {r.check_out_date}
              </div>)}
            </div>)}
            {candidates.length > 1 && <div>连接了不同住宿，请返回核对，按实际客人分别录入。</div>}
            <Button style={{ minHeight: 44 }} onClick={() => {
              resolve(withDecision("separate")); modal.destroy();
            }}>不同客人，独立创建</Button>
          </Space>,
          okText: "是续住，创建并关联", cancelText: "返回检查",
          okButtonProps: { disabled: candidates.length !== 1, style: { minHeight: 44 } },
          onOk: () => { linked.add(candidate.order_id); resolve(withDecision("link")); },
          onCancel: () => resolve(null),
          afterClose: () => resolve(null),
        });
      });
    }
    if (isDuplicateOrderError(error)) {
      return new Promise((resolve) => {
        Modal.confirm({ title: "疑似重复订单", content: extractErrorMessage(error),
          okText: "确为不同客人，继续创建", cancelText: "返回检查",
          okButtonProps: { danger: true },
          onOk: () => resolve({ ...payload, allow_duplicate: true }),
          onCancel: () => resolve(null), afterClose: () => resolve(null),
        });
      });
    }
    return null;
  };
}
