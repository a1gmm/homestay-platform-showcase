"use client";

import React, { useEffect, useState } from "react";
import { Modal, Radio, Empty, Space, Tag, message } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ordersApi } from "@/lib/api";
import { invalidateOrderWithAudit } from "@/lib/order-cache";
import { extractErrorMessage } from "@/lib/api-errors";
import { tokens } from "@/lib/design-tokens";

interface Props {
  open: boolean;
  order: any;
  onClose: () => void;
}

interface Candidate {
  room_id: string;
  next_order_id: string;
  guest_name: string;
  check_in_date: string;
}

// 续住关联（软关联）：把不换房续住的续住单和本单拴成一段连续入住。
// 一个门锁密码、押金不重复、保洁费只在末段收、看板画成一条连续横条；两张单都保留、各段账独立。
export function LinkStayModal({ open, order, onClose }: Props) {
  const queryClient = useQueryClient();
  const [picked, setPicked] = useState<string | undefined>(undefined);

  const { data: candidates = [], isLoading } = useQuery({
    queryKey: ["link-candidates", order?.order_id],
    queryFn: () => ordersApi.linkCandidates(order.order_id) as Promise<Candidate[]>,
    enabled: open && !!order?.order_id,
  });

  useEffect(() => {
    if (open) setPicked(undefined);
  }, [open]);

  const linkMutation = useMutation({
    mutationFn: () => ordersApi.linkContinuation(order.order_id, picked as string),
    onSuccess: () => {
      message.success("已关联续住：客人一个密码住到底，押金和保洁只在最后退房算一次");
      // 统一走 invalidateOrderWithAudit 收口（#50 老坑：这里曾手写清单漏了 ["stay-group"]
      // ——它不在 ["order"]/["orders"] 前缀下 → 关联成功后开着的抽屉整段头部/分段明细/
      // 「看着像续住」提示条不刷新，前台以为没关上、再点一次得到后端 400）。
      // helper 已含 orders/order/stay-group/rooms(甘特前缀)/dashboard + 本单审计，且有哨兵测试。
      invalidateOrderWithAudit(queryClient, order?.order_id);
      onClose();
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "关联失败")),
  });

  return (
    <Modal
      open={open}
      title="关联续住（同房续住拴成一段）"
      onCancel={onClose}
      onOk={() => {
        if (!picked) {
          message.warning("请选择要关联的续住单");
          return;
        }
        const c = candidates.find((x) => x.next_order_id === picked);
        const baseName = (order?.guest_name || "").trim();
        const candName = (c?.guest_name || "").trim();
        // 不同名（如夫妻各定一单）二次确认——把「规避重名误配」的责任落到人点确认这一步。
        // 仅在两边名字都已知且确实不同时才提示；缺名无法有意义比较，不打扰。
        if (c && baseName && candName && candName !== baseName) {
          Modal.confirm({
            title: "两张单客名不同，确认是同一批客人的续住？",
            content: `本单「${baseName}」将与「${(c.guest_name || "").trim()}」拴成一段连续入住（同房 ${c.room_id}、日期首尾相连）。确认前请核对确为同一批客人（如夫妻用两个名字各定一单）。`,
            okText: "确认是同一批客人",
            cancelText: "再想想",
            onOk: () => linkMutation.mutate(),
          });
          return;
        }
        linkMutation.mutate();
      }}
      okText="确认关联"
      cancelText="取消"
      okButtonProps={{ disabled: !picked }}
      confirmLoading={linkMutation.isPending}
    >
      <div style={{ marginBottom: 12, fontSize: 13, color: tokens.color.text.secondary }}>
        客人不换房续住、但续住是另一张单（常见于携程续住，或<b>夫妻用两个名字各定一单</b>）时用这个：
        把两张单拴成<b>一段连续入住</b>。列出的是同房间、退房日紧接下一段的单，<b>名字不同也会列出</b>，
        请核对确为同一批客人后再关联。
        <br />
        关联后：门锁<b>沿用原密码</b>延到最后退房日、押金<b>只收一次</b>、保洁费只在<b>最后退房</b>时收。
        两张单都保留，各自渠道/佣金分开记账，不影响对账。
      </div>
      {isLoading ? (
        <div style={{ padding: 24, textAlign: "center", color: tokens.color.text.tertiary }}>加载候选中…</div>
      ) : candidates.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="没找到可关联的续住单（需同房间、退房日紧接着下一段；已取消的单不算）"
        />
      ) : (
        <Radio.Group value={picked} onChange={(e) => setPicked(e.target.value)} style={{ width: "100%" }}>
          <Space direction="vertical" style={{ width: "100%" }}>
            {candidates.map((c) => {
              const bn = (order?.guest_name || "").trim();
              const cn = (c.guest_name || "").trim();
              const diffName = !!bn && !!cn && bn !== cn;
              return (
                <Radio key={c.next_order_id} value={c.next_order_id}>
                  {c.guest_name} · 房间 {c.room_id} · 入住 {c.check_in_date}
                  {diffName && (
                    <Tag color="orange" style={{ marginLeft: 6 }}>名字不同·请核对</Tag>
                  )}
                  <span style={{ color: tokens.color.text.tertiary, marginLeft: 6 }}>（单号 {c.next_order_id}）</span>
                </Radio>
              );
            })}
          </Space>
        </Radio.Group>
      )}
    </Modal>
  );
}
