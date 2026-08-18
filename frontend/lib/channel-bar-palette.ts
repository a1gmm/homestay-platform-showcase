// 甘特图订单条「渠道分色」专用配色（C2 淡底浓条方案，客户 2026-07-07 拍板）。
//
// 为什么不复用 channels.ts 的 color/bgColor：
//  - 列表里的小 chip 走 channels.ts 的饱和 color（小面积用鲜色没问题，保持渠道识别）；
//  - 甘特图是大色块铺满一屏，鲜色会「花、扎眼」。这里单独一套「淡彩底 + 浓色条 + 深字」，
//    底色负责耐看、左侧浓条负责一眼识别渠道、深字保证读得清。
//  - 单一色源，禁止在组件里再散落硬编码条颜色。新渠道加在这里即可。

export interface ChannelBarColor {
  body: string; // 条底色（淡彩）
  cap: string;  // 左侧浓色条（饱和渠道色）
  text: string; // 文字（同色系深色，压在淡彩底上读得清）
}

const BAR_PALETTE: Record<string, ChannelBarColor> = {
  meituan_hotel:    { body: "#F4E6CB", cap: "#E39A2E", text: "#7E5416" },
  ctrip:            { body: "#DCE7F4", cap: "#2F72C0", text: "#315B84" },
  qunar:            { body: "#F6DDD4", cap: "#DE5B41", text: "#9E4530" },
  tongcheng:        { body: "#D6EDEF", cap: "#1FA2AC", text: "#1A626A" },
  meituan_homestay: { body: "#F2EAC1", cap: "#D8B62E", text: "#6E5A12" },
  zhixing:          { body: "#F7E1CD", cap: "#E27B2E", text: "#914E1B" },
  douyin:           { body: "#E2E2E7", cap: "#3A3A44", text: "#33343E" },
  self_acquired:    { body: "#E8E1F3", cap: "#7C5FC0", text: "#574784" },
  offline:          { body: "#E9E6DF", cap: "#8C8474", text: "#5C574E" },
  self_used:        { body: "#DBEADC", cap: "#3E8F52", text: "#2E6238" },
  trial_stay:       { body: "#D6EFEA", cap: "#159E86", text: "#0F6E56" },
  fliggy:           { body: "#F6E0D2", cap: "#E36B7A", text: "#93502E" },

  // legacy 值兜底（老订单展示用，颜色借相近 active 渠道）
  meituan: { body: "#F4E6CB", cap: "#E39A2E", text: "#7E5416" },
  tujia:   { body: "#DBEADC", cap: "#3E8F52", text: "#2E6238" },
  private: { body: "#E8E1F3", cap: "#7C5FC0", text: "#574784" },
  walk_in: { body: "#E9E6DF", cap: "#8C8474", text: "#5C574E" },
  direct:  { body: "#E8E1F3", cap: "#7C5FC0", text: "#574784" },
};

// 未知渠道兜底：暖灰，不崩且不误导为某个真实渠道。
const FALLBACK_BAR: ChannelBarColor = { body: "#ECE8E0", cap: "#9A9484", text: "#5C574E" };

export function getChannelBarColors(code: string | null | undefined): ChannelBarColor {
  if (!code) return FALLBACK_BAR;
  return BAR_PALETTE[code] ?? FALLBACK_BAR;
}
