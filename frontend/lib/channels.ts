// 渠道字典统一来源。所有前端 channel 标签/色块走这里，禁止再散落硬编码。
// 客户 2026-05-23 拍板的 10 个 active 渠道 + 5 个 legacy 兼容值 + 2026-05-25 新增 trial_stay。

export interface ChannelMeta {
  code: string;
  label: string;
  color: string;     // 主色（chip 文字 / 图标）
  bgColor: string;   // 浅色背景（chip 背景）
  isLegacy?: boolean;
}

// 单一字典源。新渠道加在这里，所有组件自动生效。
export const CHANNEL_META: Record<string, ChannelMeta> = {
  // ─── 10 个 active 渠道 ───────────────────────────────────
  meituan_hotel:    { code: "meituan_hotel",    label: "美团酒店", color: "#FFB400", bgColor: "#FFF7E0" },
  douyin:           { code: "douyin",           label: "抖音",     color: "#161823", bgColor: "#F0F0F0" },
  offline:          { code: "offline",          label: "线下渠道", color: "#6B7280", bgColor: "#F3F4F6" },
  self_acquired:    { code: "self_acquired",    label: "自来客",   color: "#8B5CF6", bgColor: "#F5F3FF" },
  ctrip:            { code: "ctrip",            label: "携程",     color: "#2577E3", bgColor: "#E0EDFF" },
  qunar:            { code: "qunar",            label: "去哪儿",   color: "#FF5C40", bgColor: "#FFE8E0" },
  zhixing:          { code: "zhixing",          label: "智行",     color: "#FF6B00", bgColor: "#FFEFE0" },
  tongcheng:        { code: "tongcheng",        label: "同程",     color: "#00B5E2", bgColor: "#E0F6FB" },
  meituan_homestay: { code: "meituan_homestay", label: "美团民宿", color: "#FFD600", bgColor: "#FFFBE0" },
  self_used:        { code: "self_used",        label: "自住",     color: "#047857", bgColor: "#D1FAE5" },
  trial_stay:       { code: "trial_stay",       label: "试住",     color: "#0F766E", bgColor: "#CCFBF1" },
  fliggy:           { code: "fliggy",           label: "飞猪",     color: "#FF6A00", bgColor: "#FFEEDC" },

  // ─── Legacy（前端下拉隐藏，仅兼容老订单展示） ────────────
  meituan: { code: "meituan", label: "美团（旧）", color: "#FFB400", bgColor: "#FFF7E0", isLegacy: true },
  tujia:   { code: "tujia",   label: "途家",       color: "#22C55E", bgColor: "#DCFCE7", isLegacy: true },
  private: { code: "private", label: "私域",       color: "#8B5CF6", bgColor: "#F5F3FF", isLegacy: true },
  walk_in: { code: "walk_in", label: "散客",       color: "#6B7280", bgColor: "#F3F4F6", isLegacy: true },
  direct:  { code: "direct",  label: "自助",       color: "#8B5CF6", bgColor: "#F5F3FF", isLegacy: true },
};

// 按客户给的顺序排列，用于下拉选项。
export const ACTIVE_CHANNELS: ChannelMeta[] = [
  CHANNEL_META.meituan_hotel,
  CHANNEL_META.douyin,
  CHANNEL_META.offline,
  CHANNEL_META.self_acquired,
  CHANNEL_META.ctrip,
  CHANNEL_META.qunar,
  CHANNEL_META.zhixing,
  CHANNEL_META.tongcheng,
  CHANNEL_META.meituan_homestay,
  CHANNEL_META.fliggy,
  CHANNEL_META.trial_stay,
  CHANNEL_META.self_used,
];

// 一个未知 code 兜底（理论不应出现，避免 UI 崩）
const FALLBACK_META: ChannelMeta = {
  code: "",
  label: "—",
  color: "#6B7280",
  bgColor: "#F3F4F6",
};

export function getChannelMeta(code: string | null | undefined): ChannelMeta {
  if (!code) return FALLBACK_META;
  return CHANNEL_META[code] ?? { ...FALLBACK_META, code, label: code };
}

export function getChannelLabel(code: string | null | undefined): string {
  return getChannelMeta(code).label;
}

// 兼容旧 API：保留 CHANNEL_LABELS / CHANNEL_LABEL Record 形态供已有组件 import。
// 包含 active + legacy 全部 15 个，让老订单也能正确显示标签（不出现 raw code）。
export const CHANNEL_LABELS: Record<string, string> = Object.fromEntries(
  Object.entries(CHANNEL_META).map(([k, v]) => [k, v.label])
);
export const CHANNEL_LABEL = CHANNEL_LABELS;

// 平台型渠道（按抽佣 / 平台订单号字段判定用）—— 给 orders/new 等表单"是否显示佣金率"用
export const PLATFORM_CHANNELS = new Set<string>([
  "ctrip", "meituan_hotel", "meituan_homestay", "douyin",
  "qunar", "zhixing", "tongcheng", "fliggy",
  // legacy 平台值（老数据兼容）
  "meituan", "tujia",
]);
