import { createElement } from "react";
import type { MenuProps } from "antd";
import { LockOutlined, ToolOutlined, StopOutlined } from "@ant-design/icons";

// 「点房号直接锁房」共用逻辑：房卡 + 甘特图行菜单都用这一组锁房项，
// 一步直达锁房弹窗（page.tsx 的 setBlockingRoom + setBlockType）。
// 注意：锁房走按日期的 room-blocks 机制，不碰静态房态枚举（两套真相源雷区）。
// 用 createElement 而非 JSX，让本模块保持纯 .ts，可被 vitest 直接测。
export type LockBlockType = "reserved" | "maintenance" | "other";

export const LOCK_ACTIONS: {
  blockType: LockBlockType;
  label: string;
  icon: typeof LockOutlined;
}[] = [
  { blockType: "reserved", label: "保留房", icon: LockOutlined },
  { blockType: "maintenance", label: "维修房", icon: ToolOutlined },
  { blockType: "other", label: "停用房", icon: StopOutlined },
];

export function buildLockMenuItems(
  onLock: (blockType: LockBlockType) => void
): NonNullable<MenuProps["items"]> {
  return LOCK_ACTIONS.map((a) => ({
    key: `lock:${a.blockType}`,
    icon: createElement(a.icon),
    label: a.label,
    onClick: () => onLock(a.blockType),
  }));
}
