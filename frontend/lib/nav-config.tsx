import type { ReactNode } from "react";
import {
  DashboardOutlined,
  HomeOutlined,
  FileTextOutlined,
  TeamOutlined,
  DollarOutlined,
  CheckSquareOutlined,
  FileDoneOutlined,
  SettingOutlined,
  RobotOutlined,
} from "@ant-design/icons";

/** 导航项单一来源。历史上桌面侧边栏(layout.tsx)与移动底栏(BottomNav.tsx)各自维护
 *  一份「角色→菜单」，注释里互相声明「严格镜像」——纯人肉同步，加菜单极易漏改一端。
 *  收敛：入口注册表 NAV_ENTRIES + 角色可达清单 navForRole(桌面顺序) + 移动空间布局
 *  ROLE_NAV 全部落此。桌面/移动是「同一集合的两种排布」，故各留一份顺序，
 *  但由 nav-config.test.ts 断言「两端集合必须相等」，把人肉镜像变成 CI 硬约束。 */

export type NavKey =
  | "dashboard"
  | "rooms"
  | "orders"
  | "guests"
  | "finance"
  | "tasks"
  | "settlements"
  | "assistant"
  | "system";

export interface NavEntry {
  key: string; // 路由
  label: string; // 底栏短标签
  full: string; // 侧边栏 / 「更多」下拉完整名
  icon: ReactNode;
}

/** 全量入口注册表（桌面用 full，移动 tab 用 label；两端共享同一 icon/route）。 */
export const NAV_ENTRIES: Record<NavKey, NavEntry> = {
  dashboard: { key: "/dashboard", label: "概览", full: "今日概览", icon: <DashboardOutlined /> },
  rooms: { key: "/rooms", label: "房态", full: "房态管理", icon: <HomeOutlined /> },
  orders: { key: "/orders", label: "订单", full: "订单管理", icon: <FileTextOutlined /> },
  guests: { key: "/guests", label: "客人", full: "客人档案", icon: <TeamOutlined /> },
  finance: { key: "/finance", label: "财务", full: "财务管理", icon: <DollarOutlined /> },
  tasks: { key: "/tasks", label: "任务", full: "运营任务", icon: <CheckSquareOutlined /> },
  settlements: { key: "/settlements", label: "结算", full: "业主结算", icon: <FileDoneOutlined /> },
  assistant: { key: "/assistant", label: "问一下", full: "经营助手", icon: <RobotOutlined /> },
  system: { key: "/system", label: "系统", full: "系统管理", icon: <SettingOutlined /> },
};

/** 角色标签（顶栏/侧边栏用户信息展示）。 */
export const ROLE_LABEL: Record<string, string> = {
  admin: "管理员",
  operator: "运营",
  finance: "财务",
  cleaner: "保洁",
  keeper: "管家",
  owner: "业主",
};

/** 角色 → 可达导航项（桌面侧边栏顺序）。移动端集合必须与此一致（见 ROLE_NAV + 测试）。 */
const NAV_BY_ROLE: Record<string, NavKey[]> = {
  admin: ["dashboard", "rooms", "orders", "guests", "finance", "tasks", "settlements", "assistant", "system"],
  operator: ["dashboard", "rooms", "orders", "tasks"],
  finance: ["dashboard", "finance", "settlements"],
  owner: ["dashboard", "finance", "settlements"],
  // 保洁/管家有独立 staff 端口(layout 会重定向走)，此处仅纵深防御兜底。
  cleaner: ["dashboard", "tasks"],
  keeper: ["dashboard", "tasks"],
};

/** 未知/未来角色兜底：最小只读集（概览/房态/任务），least-privilege。桌面移动共用。
 *  注：此前桌面侧边栏对未知角色回退到含 orders/guests/finance 的 BASE，属越权面，
 *  本次收敛统一收紧到与移动底栏一致的最小集。 */
export const NAV_FALLBACK: NavKey[] = ["dashboard", "rooms", "tasks"];

/** 角色 → 桌面侧边栏导航项（有序）。 */
export function navForRole(role: string | undefined): NavKey[] {
  return (role && NAV_BY_ROLE[role]) || NAV_FALLBACK;
}

export interface RoleNav {
  fab: boolean; // 中央「开单」FAB —— 仅能开单的角色(admin/operator)显示
  left: NavKey[]; // FAB 左侧 tab
  right: NavKey[]; // FAB 右侧 tab
  more: NavKey[]; // 「更多」下拉
}

/** 角色 → 移动底栏空间布局。集合(left+right+more)必须 == navForRole(role)（测试守卫）。
 *  FAB 居中依赖「左侧 tab 数 == 右侧 tab 数」—— admin/operator 均为 2:1(+更多算右)。 */
export const ROLE_NAV: Record<string, RoleNav> = {
  admin: {
    fab: true,
    left: ["dashboard", "rooms"],
    right: ["tasks"],
    more: ["guests", "orders", "finance", "settlements", "assistant", "system"],
  },
  operator: {
    fab: true,
    left: ["dashboard", "rooms"],
    right: ["tasks"],
    more: ["orders"],
  },
  finance: { fab: false, left: ["dashboard", "finance", "settlements"], right: [], more: [] },
  owner: { fab: false, left: ["dashboard", "finance", "settlements"], right: [], more: [] },
  cleaner: { fab: false, left: ["dashboard", "tasks"], right: [], more: [] },
  keeper: { fab: false, left: ["dashboard", "tasks"], right: [], more: [] },
};

/** 移动底栏未知角色兜底（与 NAV_FALLBACK 集合一致）。 */
export const FALLBACK_NAV: RoleNav = {
  fab: false,
  left: ["dashboard", "rooms", "tasks"],
  right: [],
  more: [],
};
