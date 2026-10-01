import type { ReactNode } from "react";
import {
  DashboardOutlined,
  HomeOutlined,
  FileTextOutlined,
  TeamOutlined,
  DollarOutlined,
  CheckSquareOutlined,
  FileDoneOutlined,
  FolderOpenOutlined,
  SettingOutlined,
  RobotOutlined,
  FileSearchOutlined,
  PieChartOutlined,
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
  | "monthlyClose"
  | "utilityRecon"
  | "tasks"
  | "settlements"
  | "content"
  | "assistant"
  | "system";

export type NavGroupKey = "today" | "operations" | "finance" | "system";

export type SecondaryNavKey =
  | "billing_recon"
  | "share_config"
  | "system_users"
  | "system_audit";

export interface NavEntry {
  key: string; // 路由
  label: string; // 底栏短标签
  full: string; // 侧边栏 / 「更多」下拉完整名
  description: string;
  group: NavGroupKey;
  icon: ReactNode;
  parent?: NavKey;
}

export const NAV_GROUPS: ReadonlyArray<{ key: NavGroupKey; label: string }> = [
  { key: "today", label: "今日经营" },
  { key: "operations", label: "运营执行" },
  { key: "finance", label: "财务结算" },
  { key: "system", label: "系统管理" },
];

/** 全量入口注册表（桌面用 full，移动 tab 用 label；两端共享同一 icon/route）。 */
export const NAV_ENTRIES: Record<NavKey, NavEntry> = {
  dashboard: { key: "/dashboard", label: "概览", full: "今日概览", description: "查看今日入住、退房、收入和待办", group: "today", icon: <DashboardOutlined /> },
  rooms: { key: "/rooms", label: "房态", full: "房态管理", description: "查看空房、在住、保洁和排房情况", group: "today", icon: <HomeOutlined /> },
  orders: { key: "/orders", label: "订单", full: "订单管理", description: "查询订单并办理入住、退房与收款", group: "today", icon: <FileTextOutlined /> },
  guests: { key: "/guests", label: "客人", full: "客人档案", description: "查询客人资料和历史入住记录", group: "today", icon: <TeamOutlined /> },
  finance: { key: "/finance", label: "财务", full: "财务管理", description: "查看收入、支出、费用和经营数据", group: "finance", icon: <DollarOutlined /> },
  monthlyClose: { key: "/finance/monthly-close", label: "月结", full: "月结助理", description: "按资料和问题完成每月对账与业主结算", group: "finance", icon: <FileDoneOutlined /> },
  utilityRecon: { key: "/finance/utility-recon", label: "水电对账", full: "水电费对账", description: "核对房间水电账单和费用差异", group: "finance", parent: "monthlyClose", icon: <DollarOutlined /> },
  tasks: { key: "/tasks", label: "任务", full: "运营任务", description: "跟进保洁、维修和现场待办", group: "operations", icon: <CheckSquareOutlined /> },
  settlements: { key: "/settlements", label: "结算", full: "业主结算", description: "生成、核对和查看月度业主结算", group: "finance", icon: <FileDoneOutlined /> },
  content: { key: "/content/owner", label: "内容", full: "内容中心", description: "维护业主服务、入住指南和旅行内容", group: "operations", icon: <FolderOpenOutlined /> },
  assistant: { key: "/assistant", label: "问一下", full: "经营助手", description: "查询经营数据并获取运营分析", group: "operations", icon: <RobotOutlined /> },
  system: { key: "/system", label: "系统", full: "系统管理", description: "管理业主分成、账号权限和审计记录", group: "system", icon: <SettingOutlined /> },
};

/** 二级业务入口也在同一文件注册；parent 决定桌面高亮归属。 */
export const SECONDARY_NAV_ENTRIES: Record<SecondaryNavKey, NavEntry> = {
  billing_recon: {
    key: "/finance/billing-recon", label: "对账", full: "账单对账",
    description: "导入平台账单并处理系统差异", group: "finance",
    parent: "monthlyClose", icon: <FileSearchOutlined />,
  },
  share_config: {
    key: "/system/share-config", label: "分成", full: "业主与分成",
    description: "关联房间业主并设置分成和费用比例", group: "system",
    parent: "system", icon: <PieChartOutlined />,
  },
  system_users: {
    key: "/system/users", label: "账号", full: "账号与权限",
    description: "开通、禁用账号并管理登录权限", group: "system",
    parent: "system", icon: <TeamOutlined />,
  },
  system_audit: {
    key: "/system/audit", label: "审计", full: "审计日志",
    description: "追溯关键业务和配置操作", group: "system",
    parent: "system", icon: <FileSearchOutlined />,
  },
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
  admin: ["dashboard", "rooms", "orders", "guests", "finance", "tasks", "monthlyClose", "content", "assistant", "system"],
  operator: ["dashboard", "rooms", "orders", "tasks", "monthlyClose", "content"],
  finance: ["dashboard", "finance", "monthlyClose", "settlements"],
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

const SECONDARY_BY_ROLE: Record<string, SecondaryNavKey[]> = {
  admin: ["share_config", "system_users", "system_audit"],
  finance: [],
  owner: [],
  operator: [],
  cleaner: [],
  keeper: [],
};

export interface NavGroup {
  key: NavGroupKey;
  label: string;
  entries: NavEntry[];
}

function groupEntries(entries: NavEntry[]): NavGroup[] {
  return NAV_GROUPS.map((group) => ({
    ...group,
    entries: entries.filter((entry) => entry.group === group.key),
  })).filter((group) => group.entries.length > 0);
}

/** 桌面侧栏只放一级工作区；二级动作在“全部功能”和各工作区内部出现。 */
export function groupedDesktopNav(role: string | undefined): NavGroup[] {
  return groupEntries(navForRole(role).map((key) => NAV_ENTRIES[key]));
}

/** “全部功能”同时收录获授权的一级工作区与直接可执行的二级动作。 */
export function allFeaturesForRole(role: string | undefined): NavGroup[] {
  const primary = navForRole(role).map((key) => NAV_ENTRIES[key]);
  const secondaryKeys = (role && SECONDARY_BY_ROLE[role]) || [];
  const secondary = secondaryKeys.map((key) => SECONDARY_NAV_ENTRIES[key]);
  return groupEntries([...primary, ...secondary]);
}

export function featurePathsForRole(role: string | undefined): string[] {
  return allFeaturesForRole(role).flatMap((group) => group.entries.map((entry) => entry.key));
}

/** 二级页面按 parent 回到一级工作区；最长路径优先，避免前缀误匹配。 */
export function activeNavPath(pathname: string): string {
  const entries = [
    ...Object.values(SECONDARY_NAV_ENTRIES),
    ...Object.values(NAV_ENTRIES),
  ].sort((a, b) => b.key.length - a.key.length);
  const match = entries.find((entry) => pathname === entry.key || pathname.startsWith(`${entry.key}/`));
  return match?.parent ? NAV_ENTRIES[match.parent].key : match?.key || "/dashboard";
}

export interface RoleNav {
  fab: boolean; // 中央「开单」FAB —— 仅能开单的角色(admin/operator)显示
  left: NavKey[]; // FAB 左侧 tab
  right: NavKey[]; // FAB 右侧 tab
  more: NavKey[]; // 「更多」下拉
}

/** 角色 → 移动底栏空间布局。left/right 是高频 tab，more 是“全部功能”承接的低频项。
 *  集合(left+right+more)必须 == navForRole(role)（测试守卫）。FAB 浮在底栏上方，不占 tab。 */
export const ROLE_NAV: Record<string, RoleNav> = {
  admin: {
    fab: true,
    left: ["dashboard", "rooms"],
    right: ["orders", "tasks"],
    more: ["guests", "finance", "monthlyClose", "content", "assistant", "system"],
  },
  operator: {
    fab: true,
    left: ["dashboard", "rooms"],
    right: ["orders", "tasks"],
    more: ["monthlyClose", "content"],
  },
  finance: { fab: false, left: ["dashboard", "finance", "settlements"], right: [], more: ["monthlyClose"] },
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
