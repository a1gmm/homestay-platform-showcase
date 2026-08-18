// 甘特图订单条「完成度分色」——单一色源。
//
// 背景：前台盯房态甘特，真正要找的不是「哪个渠道」，而是「哪些订单的入住还没办完」，
// 好知道还剩几个任务要做（前台 2026-07-11 拍板「方案 1」）。所以这套配色的主轴是
// **完成度**，不是渠道：
//   - 待确认·还没确认订单 → 琥珀实心 + 闪点，一眼跳出来「这单要去确认」；
//   - 已确认·待入住      → 浅琥珀金，跟待确认同暖色家族（都属入住前），但安静下来
//                          ——点完「确认订单」后就从琥珀变这色，表示确认这步办完了、等客人来；
//   - 在住·已办完入住     → 明确的绿 + ✓，与暖琥珀「没入住」一眼分开；
//   - 待退房            → 浅蓝（已入住，但今天要退，另一类任务）；
//   - 已完成            → 灰，过去时；
//   - 已取消            → 灰 + 划掉。
//
// 为什么单独一套、且集中在这里：桌面 GanttView / 移动 MobileGantt 两处前台甘特都读这里，
// 避免把这套颜色再散落硬编码到组件里（历史上 STATUS_COLOR 三份硬编码难同步的坑）。
// 业主 OwnerGanttView 仍按渠道分色，不走这套。

export type CompletionBucket =
  | "todo"
  | "confirmed"
  | "in_house"
  | "checkout"
  | "done"
  | "cancelled";

/**
 * 按订单状态归到「完成度」桶。
 * - 待确认（pending_confirm）→ todo：前台还没点「确认订单」，最要紧的活，亮橙跳出来。
 * - 已确认待入住（paid_pending_room/roomed_pending_checkin）→ confirmed：确认这步办完了，
 *   等客人来，给个安静的区分色（点完确认订单颜色即变，一眼看出办没办确认）。
 * - pending_payment（待完成，存量在途单出口，新单不进入）仍当待办兜到 todo。
 * 未知/空状态兜底当 todo：宁可多提醒一次，也别把一个没处理的单藏进「已办」里漏掉。
 */
export function completionBucketOf(status?: string | null): CompletionBucket {
  switch (status) {
    case "pending_confirm":
    case "pending_payment":
      return "todo";
    case "paid_pending_room":
    case "roomed_pending_checkin":
      return "confirmed";
    case "checked_in":
      return "in_house";
    case "pending_checkout":
      return "checkout";
    case "completed":
      return "done";
    case "cancelled":
      return "cancelled";
    default:
      return "todo";
  }
}

export interface CompletionBarStyle {
  /** 条底色 */
  body: string;
  /** 文字色 */
  text: string;
  /** 条内小标文案（取消态不出标） */
  badge?: string;
  /** 未办：加闪动小点，进一步提醒「这单还没办」 */
  pulse?: boolean;
  /** 取消态：整条置灰 + 划掉 */
  cancelled?: boolean;
}

const STYLE: Record<CompletionBucket, CompletionBarStyle> = {
  // 待确认：中饱和琥珀 + 深棕字 + 闪点。仍是屏幕上最亮的色（还亮着几块 = 还剩几个没确认），
  // 但压掉了原「饱和亮橙#F59E0B+白字」——OTA 单进来默认全是待确认，满屏亮橙前台盯久了刺眼
  //（前台太阳 2026-07-30 反馈，王总拍板换色）。
  todo: { body: "#F2C14E", text: "#5F430A", badge: "待确认", pulse: true },
  // 已确认·待入住：浅琥珀金底 + 深棕字。跟待确认同暖色家族（都属「没入住」），但去掉饱和与闪点，
  // 表示「确认订单」这步已办、只等客人来——点完确认订单条子就从亮橙变到这色。
  confirmed: { body: "#F8E9C9", text: "#7A5A12", badge: "已确认·待入住" },
  // 在住：明确的绿底 + 深绿字 + ✓。原 #DCEAE0 淡得近灰，跟「待入住」淡杏在普通屏上分不出
  // ——「没入住=暖琥珀 / 已入住=绿」是前台要的核心区分，绿要绿得出来（同一次反馈）。
  in_house: { body: "#A9D8B8", text: "#1D5A36", badge: "✓ 已入住" },
  // 待退房：浅蓝底 + 深蓝字。已入住但今天要退，属另一类待办，给个区分色但不抢「待入住」的戏。
  checkout: { body: "#C9DFF6", text: "#2C5A88", badge: "待退房" },
  // 已完成：中性灰，过去时。
  done: { body: "#EAE8E2", text: "#8A857C", badge: "已完成" },
  // 已取消：暖灰 + 划掉，不误认为某个真实状态。
  cancelled: { body: "#E7E4DF", text: "#8A857C", cancelled: true },
};

/** 给订单状态返回完成度色块样式。block（维修/锁房等）不走这里，由组件另行处理。 */
export function getCompletionBarStyle(status?: string | null): CompletionBarStyle {
  return STYLE[completionBucketOf(status)];
}

/**
 * 续住组横条「整条统一取色」该用哪个状态 —— 与后端 group_view 口径 1 对齐：
 *   任一段待退房 → 待退房；否则任一段在住 → 在住；否则取锚单（最早段）状态。
 *
 * 背景：续住是多张**独立**订单软关联成一条连续横条，各段各留自己的 order_status。
 * 后端刻意让中间段停在 checked_in、只有末段先进 pending_checkout（只有末段能退房，
 * 完成时才 complete_group_siblings 级联全组），所以「绿一半 + 蓝一半」的裸状态本身是对的。
 * 但订单详情页走 group_view 显示的是**整组统一状态**；甘特若逐格铺裸状态就会画成一条花条，
 * 跟详情页对不上。此函数把甘特取色接到同一口径，消除「花条」，不改任何 order_status。
 *
 * 入参 = 横条上各格状态，按入住先后（左→右）排列；连续同段会重复，第一个即锚单状态。
 * 已取消段不进甘特日历（后端已 not_in([cancelled])），此处再兜一层过滤不影响结果。
 */
export function stayGroupBarStatus(
  statusesInOrder: (string | null | undefined)[],
): string | null | undefined {
  const alive = statusesInOrder.filter((s) => s != null && s !== "cancelled");
  if (alive.length === 0) return statusesInOrder[0];
  if (alive.includes("pending_checkout")) return "pending_checkout";
  if (alive.includes("checked_in")) return "checked_in";
  return alive[0];
}

export interface CompletionLegendItem {
  body: string;
  /** 有边框色时图例小块描边（浅底色需要描边才看得清） */
  border?: string;
  label: string;
}

// 图例：前台要认的几类。顺序 = 待办优先（最要紧的排前面）。
export const COMPLETION_LEGEND: CompletionLegendItem[] = [
  { body: STYLE.todo.body, label: "待确认·还没确认订单" },
  { body: STYLE.confirmed.body, border: "#E6C98A", label: "已确认·待入住" },
  { body: STYLE.in_house.body, border: "#7CBB96", label: "在住·已办完" },
  { body: STYLE.checkout.body, border: "#A6C6E8", label: "待退房" },
  { body: STYLE.cancelled.body, border: "#D8D3CA", label: "已取消" },
];
