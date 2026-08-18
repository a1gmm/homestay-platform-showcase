import type { OrderFilters } from "@/hooks/useOrders";

/**
 * URL query → 订单列表筛选态。
 * 抽成纯函数是为了能单测，也让 page 的初始化与 URL 变化两条路径共用同一份逻辑
 * （原来只在挂载时读一次，同路由跳转时不生效——点第二条新订单 toast 没反应）。
 */
export function filtersFromSearchParams(p: URLSearchParams): OrderFilters {
  return {
    status: p.get("status") || undefined,
    channel: p.get("channel") || undefined,
    keyword: p.get("keyword") || undefined,
    check_in_from: p.get("check_in_from") || undefined,
    check_in_to: p.get("check_in_to") || undefined,
    check_out_from: p.get("check_out_from") || undefined,
    check_out_to: p.get("check_out_to") || undefined,
  };
}
