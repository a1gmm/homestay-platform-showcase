"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { message } from "antd";
import { roomBlocksApi } from "@/lib/api";
import type { RoomOut } from "@/lib/types";

// issue#5 锁房：三类可创建（停用/保留/维修，对应后端 RoomBlock.block_type）
export type BlockType = "other" | "reserved" | "maintenance";

export const BLOCK_TYPE_LABELS: Record<BlockType, string> = {
  other: "停用房",
  reserved: "保留房",
  maintenance: "维修房",
};

// 展示用：覆盖全部 4 种锁房类型（含房东自住，可能来自业主端创建）
export const BLOCK_TYPE_DISPLAY: Record<string, string> = {
  other: "停用房",
  reserved: "保留房",
  maintenance: "维修房",
  owner_use: "房东自住",
};

// 锁房表单 state + 创建/解除 mutation 的集中封装。
// 业务逻辑归 hooks（见 CLAUDE.md 组件架构原则），Modal 只做渲染。
export function useRoomBlocking() {
  const qc = useQueryClient();

  const [blockingRoom, setBlockingRoom] = useState<RoomOut | null>(null);
  const [blockType, setBlockType] = useState<BlockType | null>(null);
  const [blockRange, setBlockRange] = useState<[any, any] | null>(null);
  const [blockNoEnd, setBlockNoEnd] = useState(false);
  const [blockReason, setBlockReason] = useState("");

  const resetBlockForm = () => {
    setBlockRange(null);
    setBlockNoEnd(false);
    setBlockReason("");
  };

  // 关闭锁房表单并复位（Modal onCancel / onSuccess 都走这里）
  const closeBlockForm = () => {
    setBlockingRoom(null);
    setBlockType(null);
    resetBlockForm();
  };

  const blockMutation = useMutation({
    mutationFn: (data: {
      room_id: string;
      block_type: BlockType;
      start_date: string;
      end_date: string;
      reason?: string;
    }) => roomBlocksApi.create(data as any),
    onSuccess: () => {
      message.success("锁房记录已创建");
      qc.invalidateQueries({ queryKey: ["rooms"] });
      qc.invalidateQueries({ queryKey: ["rooms", "calendar"] });
      closeBlockForm();
    },
    // 错误提示走 providers.tsx 全局 mutationCache.onError（extractErrorMessage）。
    // 之前这里把 422 的 detail 对象数组直接塞 message.error 导致渲染崩溃，
    // 员工看不到任何报错（2026-06-12 生产事故）。
  });

  // 解除时间段锁房（甘特图灰条 / 房卡锁房列表都用它）
  const releaseBlockMutation = useMutation({
    mutationFn: (blockId: string) => roomBlocksApi.delete(blockId),
    onSuccess: () => {
      message.success("已解除锁房");
      qc.invalidateQueries({ queryKey: ["rooms"] });
      qc.invalidateQueries({ queryKey: ["rooms", "calendar"] });
      qc.invalidateQueries({ queryKey: ["room-blocks"] });
    },
  });

  return {
    blockingRoom, setBlockingRoom,
    blockType, setBlockType,
    blockRange, setBlockRange,
    blockNoEnd, setBlockNoEnd,
    blockReason, setBlockReason,
    resetBlockForm,
    closeBlockForm,
    blockMutation,
    releaseBlockMutation,
  };
}
