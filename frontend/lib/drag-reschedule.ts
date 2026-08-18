// 甘特拖拽「换房到空格」的确认决策（纯函数，便于测试）。
//
// 背景：拖拽合并了「换房」（垂直移动）与「改日期」（水平移动）两种能力。
// 客户诉求：做换房时日期不应被静默改动——落到不同日期列时必须弹确认，
// 提示「与实际入住时间不符」。此函数只决定是否弹、弹什么，不触发副作用。

export interface RoomBrief {
  room_id: string;
  base_price?: number | string | null;
}

export interface DragConfirmInput {
  datesChanged: boolean;
  priceDiffers: boolean;
  before: { check_in_date: string; check_out_date: string };
  next: { check_in_date: string; check_out_date: string };
  sourceRoom?: RoomBrief | null;
  targetRoom?: RoomBrief | null;
  /** 被拖订单是否已入住（已入住换房要重置门锁密码，须提示前台转发新码） */
  isCheckedIn?: boolean;
  /** 被拖订单是否已完成（已完成=退房、收入已归属结算；跨房东纠错要提示重算分账） */
  isCompleted?: boolean;
  /** 原房间所属房东 id（判分账归属是否变化） */
  sourceOwnerId?: string | null;
  /** 目标房间所属房东 id */
  targetOwnerId?: string | null;
  /** 拖过去后入住日是否落在今天之前（纠错「客人其实上周就来了」的正当场景） */
  movesToPast?: boolean;
  /** 被拖订单是否属于续住组（拖某段只动那一段，会拆断连续入住 → 需提示，不挡操作） */
  isStayGroup?: boolean;
  /** 组内段数（stay-group 接口拿到的 segments.length；拉不到传 null，提示里就不写段数） */
  staySegmentCount?: number | null;
}

export interface DateChange {
  fromCheckIn: string;
  toCheckIn: string;
  fromCheckOut: string;
  toCheckOut: string;
}

export interface DragConfirmPlan {
  needsConfirm: boolean;
  title: string;
  okText: string;
  cancelText: string;
  /** true 时应以醒目样式（警告图标 + 红色高亮 + danger 按钮）渲染 */
  danger: boolean;
  dateWarning?: string;
  /** 结构化的日期变更，供 UI 高亮新旧日期 */
  dateChange?: DateChange;
  priceWarning?: string;
  /** 已入住换房时的门锁换码提示（旧码失效、新房下新码，需转发给客人） */
  lockWarning?: string;
  /** 已完成单跨房东纠错时的分账归属提示（该月分账若已生成需重算） */
  settlementWarning?: string;
  /** 续住组某段被拖时的拆组提示（只动这一段，可能拆断连续入住；只提示不挡） */
  stayGroupWarning?: string;
  /**
   * 确认后须带 allow_past_dates=true 重发 PATCH，否则后端守卫会拦下
   * （「入住日期不能早于今天」）。仅在确实拖到过去时为 true——别无差别放开守卫。
   */
  allowPastDates: boolean;
}

export function planDragConfirm(input: DragConfirmInput): DragConfirmPlan {
  const { datesChanged, priceDiffers, before, next, sourceRoom, targetRoom } = input;

  let dateWarning: string | undefined;
  let dateChange: DateChange | undefined;
  if (datesChanged) {
    dateWarning =
      `此拖拽会改变订单日期：入住 ${before.check_in_date} → ${next.check_in_date}，` +
      `退房 ${before.check_out_date} → ${next.check_out_date}。` +
      (input.movesToPast
        ? `新的入住日 ${next.check_in_date} 已经过去了——补录/纠错老单请确认，手滑请取消。`
        : "") +
      `与实际入住时间不符，确认要修改日期吗？（仅换房请拖回原日期列）`;
    dateChange = {
      fromCheckIn: before.check_in_date,
      toCheckIn: next.check_in_date,
      fromCheckOut: before.check_out_date,
      toCheckOut: next.check_out_date,
    };
  }

  let priceWarning: string | undefined;
  if (priceDiffers && targetRoom && sourceRoom) {
    priceWarning =
      `目标 ${targetRoom.room_id} 挂牌 ¥${Number(targetRoom.base_price ?? 0)} / ` +
      `当前 ${sourceRoom.room_id} 挂牌 ¥${Number(sourceRoom.base_price ?? 0)}。` +
      `将按订单原价迁移（如需改价请到订单详情手动修改）。`;
  }

  const lockWarning = input.isCheckedIn
    ? "该客人已入住，换房会重置门锁密码：旧房密码立即失效，新房自动下发新密码。" +
      "请到密码群查看并转发给客人；若新房锁离线会标注「待确认」。"
    : undefined;

  // 已完成单=客人已退房、这段收入已按原房归到某个房东名下。拖到「不同房东」的房间纠错，
  // 收入归属会随之改变；若该月房东分账已生成，需手动重算。同房东或缺房东信息则不提示。
  const settlementWarning =
    input.isCompleted &&
    input.sourceOwnerId &&
    input.targetOwnerId &&
    input.sourceOwnerId !== input.targetOwnerId
      ? "这两间房不是同一个房东，纠正已完成订单的房号会改变这段收入的归属。" +
        "若本月房东分账已经生成，请记得重新生成一次。"
      : undefined;

  // 续住组拖拽负载是单段：拖动组内某段只动那一段，其余段不跟着走 → 可能拆断连续入住。
  // 只点破，不挡操作（拆段纠错是正当动作）。段数拉不到就不写，别显示「共 0 段」。
  const stayGroupWarning = input.isStayGroup
    ? `此单属续住组${
        input.staySegmentCount != null && input.staySegmentCount > 0
          ? `（共 ${input.staySegmentCount} 段连续入住）`
          : ""
      }，本次拖拽仅移动这一段，其余段不会跟着动，可能拆断连续入住。`
    : undefined;

  const needsConfirm =
    Boolean(dateWarning) ||
    Boolean(priceWarning) ||
    Boolean(lockWarning) ||
    Boolean(settlementWarning) ||
    Boolean(stayGroupWarning);

  // 标题/按钮按关注度分优先级：日期变更 > 已完成单跨房东纠错 > 仅挂牌价不同 > 仅拆组提示。
  const title = dateWarning
    ? "确认修改入住日期"
    : settlementWarning
      ? "确认纠正房号"
      : priceWarning
        ? "目标房间挂牌价不同"
        : stayGroupWarning
          ? "此单属于续住组"
          : "目标房间挂牌价不同";
  const okText = dateWarning
    ? "确认修改"
    : settlementWarning
      ? "确认纠正"
      : priceWarning
        ? "按原价迁移"
        : stayGroupWarning
          ? "仍要移动"
          : "按原价迁移";

  return {
    needsConfirm,
    title,
    okText,
    cancelText: "取消",
    danger: Boolean(dateWarning),
    dateWarning,
    dateChange,
    priceWarning,
    lockWarning,
    settlementWarning,
    stayGroupWarning,
    // 拖到过去必然 datesChanged=true → 必然弹确认，故「确认过」与「放行守卫」等价。
    allowPastDates: Boolean(input.movesToPast && datesChanged),
  };
}
