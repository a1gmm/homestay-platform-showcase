import type { StayGroup } from "@/lib/types";

// 续住段的按钮闸：把后端既有的 400 拦截提前成「压根不显示」。
// 后端口径见 app/api/v1/orders.py —— 中间段退房与续住段收押金各有一道 400。
// 这里只是不让前台点到那两堵墙，不是新规则。
// 无段信息时一律放行：非续住单和加载中都不该被误伤。
export function segmentActionGates(orderId: string, group?: StayGroup) {
  if (!group || !group.stay_group_id) {
    return { canCheckout: true, canCollectDeposit: true };
  }
  return {
    canCheckout: orderId === (group.checkout_order_id || group.last_order_id),
    canCollectDeposit: orderId === group.anchor_order_id,
  };
}

// The server excludes cancelled tails when choosing last_order_id. Never infer
// the destination from array order or dates (multi-room groups can overlap).
export function checkoutSegmentDestination(orderId: string, group?: StayGroup) {
  if (!group?.stay_group_id) return undefined;
  const destination = group.checkout_order_id || group.last_order_id;
  if (destination === orderId) return undefined;
  return group.segments.find(s => s.order_id === destination && s.order_status !== "cancelled");
}
