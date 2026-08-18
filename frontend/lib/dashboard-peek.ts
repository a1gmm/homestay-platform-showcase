/**
 * 今日概览 KPI 卡片「同屏速览」预设。
 *
 * 点卡片不再整页跳到订单大表，而是右侧滑出抽屉列当前这批（orders / tasks），
 * 抽屉底部「查看全部」再跳到带筛选的完整列表页。
 *
 * ⚠️ orders 的 status 口径必须与后端 dashboard._today_stats 逐字一致，
 * 否则卡上数字与抽屉/列表对不上。
 */

/**
 * orders / tasks 各自去列表 API 拉；rooms 不请求，直接渲染
 * `/dashboard/today` 已经返回的 `cleaning_list`——卡上的数字和抽屉里的名单同一份数据。
 */
export type PeekKind = "orders" | "tasks" | "rooms";

export interface PeekPreset {
  key: string;
  title: string;
  kind: PeekKind;
  /** 列表 API 的查询参数（值统一为 string，便于拼 URL） */
  apiParams: Record<string, string>;
  /** 跳转到完整列表页时显示在「已筛选」提示条上的可读标签 */
  label: string;
  /** 「查看全部」目标地址（完整列表页 + 预填筛选 + filter_label） */
  href: string;
  /** 空态文案 */
  emptyText: string;
}

/** 待入住三态：确认 / 排房后 / 待入住，均属「今日待入住」。 */
const CHECKIN_STATUSES = "pending_confirm,paid_pending_room,roomed_pending_checkin";
/** 待退房三态：在住 / 已退房待收款 / 待完成。 */
const CHECKOUT_STATUSES = "checked_in,pending_checkout,pending_payment";

function encode(params: Record<string, string>): string {
  return new URLSearchParams(params).toString();
}

export function buildOrdersHref(
  params: Record<string, string>,
  label: string
): string {
  return `/orders?${encode({ ...params, filter_label: label })}`;
}

export function buildTasksHref(
  params: Record<string, string>,
  label: string
): string {
  return `/tasks?${encode({ ...params, filter_label: label })}`;
}

/**
 * 首页下方「今日待入住」小名单的列表请求参数。
 *
 * **必须带 status**，且与 KPI 卡逐字同口径——所以直接复用 checkin 速览预设的
 * apiParams，改一处两处一起变。曾经这里是页面里内联的
 * `{ check_in_from, check_in_to }`，没有 status：凡是「入住日=今天」的单全被捞进来，
 * 按建单时间倒序取前 5 条。生产 2026-07-25 实测前 5 条 = 2 条已入住 + 2 条已取消 +
 * 1 条续住段，没有一条是真要办入住的——前台反映的「明明已经入住了还显示待入住」
 * 就是这个名单。
 */
export function buildTodayCheckinListParams(
  today: string,
  limit = 5
): Record<string, string> {
  return {
    ...buildPeekPresets(today).checkin.apiParams,
    page: "1",
    page_size: String(limit),
  };
}

export function buildPeekPresets(today: string): Record<string, PeekPreset> {
  const checkinParams = {
    status: CHECKIN_STATUSES,
    check_in_from: today,
    check_in_to: today,
    // 续住组非首段入住日=今天，但客人昨天就住进来了，不用办入住 —— 与数字卡
    // dashboard._today_stats 同口径剔除，否则卡上数字与抽屉/名单对不上
    // （生产 2026-07-25 sg_24b6b1aa12ba / 1615 房）。退房侧同款开关见下。
    exclude_continuation_later_checkin: "true",
  };
  const checkoutParams = {
    status: CHECKOUT_STATUSES,
    check_out_from: today,
    check_out_to: today,
    // 续住组中间段退房日=今天但客人不真走（去下一段续住），不算今日待退房。
    // 与数字卡 dashboard._today_stats 同口径，否则卡上数字与抽屉名单对不上
    // （生产 1616 刘丹：数字卡剔除、抽屉却漏进来）。
    exclude_continuation_mid_checkout: "true",
  };
  const cleaningParams = { status: "pending" };
  const overdueParams = { overdue_only: "true" };

  return {
    checkin: {
      key: "checkin",
      title: "今日待入住",
      kind: "orders",
      apiParams: checkinParams,
      label: "今日待入住",
      href: buildOrdersHref(checkinParams, "今日待入住"),
      emptyText: "今天没有待入住订单",
    },
    checkout: {
      key: "checkout",
      title: "今日待退房",
      kind: "orders",
      apiParams: checkoutParams,
      label: "今日待退房",
      href: buildOrdersHref(checkoutParams, "今日待退房"),
      emptyText: "今天没有待退房订单",
    },
    cleaning: {
      key: "cleaning",
      title: "待保洁房间",
      // 数字与名单同源：抽屉渲染 /dashboard/today 的 cleaning_list，不再另查 tasks 表。
      // 旧实现数字读 orders.cleaning_status、抽屉读 tasks(status=pending)，两个真相源
      // 天生对不上（生产 2026-07-25：卡上 22、抽屉 51，且 tasks 里 36 条保洁任务全无
      // deadline，是从没关闭的历史积压，跟「今天要扫哪几间」毫无关系）。
      kind: "rooms",
      apiParams: cleaningParams,
      label: "待保洁",
      href: buildTasksHref(cleaningParams, "待保洁"),
      emptyText: "今天没有需要打扫的退房房",
    },
    overdue: {
      key: "overdue",
      title: "逾期任务",
      kind: "tasks",
      apiParams: overdueParams,
      label: "逾期任务",
      href: buildTasksHref(overdueParams, "逾期任务"),
      emptyText: "没有逾期任务 🎉",
    },
  };
}
