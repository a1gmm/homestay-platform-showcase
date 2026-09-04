import type { OrderFilters } from "@/hooks/useOrders";

const BUSINESS_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function isValidBusinessDate(value: string): boolean {
  if (!BUSINESS_DATE_PATTERN.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return (
    parsed.getUTCFullYear() === year &&
    parsed.getUTCMonth() === month - 1 &&
    parsed.getUTCDate() === day
  );
}

function assertValidDateRange(dateFrom: string, dateTo: string): void {
  if (!isValidBusinessDate(dateFrom) || !isValidBusinessDate(dateTo)) {
    throw new Error("日期必须是有效的 YYYY-MM-DD");
  }
  if (dateTo < dateFrom) throw new Error("结束日期不能早于开始日期");
}

/**
 * URL query → 订单列表筛选态。
 * 抽成纯函数是为了能单测，也让 page 的初始化与 URL 变化两条路径共用同一份逻辑
 * （原来只在挂载时读一次，同路由跳转时不生效——点第二条新订单 toast 没反应）。
 */
export function filtersFromSearchParams(p: URLSearchParams): OrderFilters {
  const filters: OrderFilters = {
    status: p.get("status") || undefined,
    channel: p.get("channel") || undefined,
    keyword: p.get("keyword") || undefined,
  };

  const dateBasis = p.get("date_basis");
  const dateFrom = p.get("date_from");
  const dateTo = p.get("date_to");
  const checkInFrom = p.get("check_in_from");
  const checkInTo = p.get("check_in_to");
  const checkOutFrom = p.get("check_out_from");
  const checkOutTo = p.get("check_out_to");
  const hasCanonical = Boolean(dateBasis || dateFrom || dateTo);
  const hasLegacyCheckin = Boolean(checkInFrom || checkInTo);
  const hasLegacyCheckout = Boolean(checkOutFrom || checkOutTo);

  if (hasCanonical && (hasLegacyCheckin || hasLegacyCheckout)) {
    throw new Error("不能混用新旧日期筛选参数");
  }
  if (hasCanonical) {
    if (!dateBasis || !dateFrom || !dateTo) {
      throw new Error("date_basis、date_from、date_to 必须同时提供");
    }
    if (dateBasis !== "final_checkout" && dateBasis !== "first_checkin") {
      throw new Error("无效的日期口径");
    }
    assertValidDateRange(dateFrom, dateTo);
    return { ...filters, date_basis: dateBasis, date_from: dateFrom, date_to: dateTo };
  }

  if (hasLegacyCheckin && hasLegacyCheckout) {
    throw new Error("入住日和退房日筛选不能同时提供");
  }
  if (hasLegacyCheckin) {
    if (!checkInFrom || !checkInTo) throw new Error("入住日期起止必须同时提供");
    assertValidDateRange(checkInFrom, checkInTo);
    return {
      ...filters,
      date_basis: "first_checkin",
      date_from: checkInFrom,
      date_to: checkInTo,
    };
  }
  if (hasLegacyCheckout) {
    if (!checkOutFrom || !checkOutTo) throw new Error("退房日期起止必须同时提供");
    assertValidDateRange(checkOutFrom, checkOutTo);
    return {
      ...filters,
      date_basis: "final_checkout",
      date_from: checkOutFrom,
      date_to: checkOutTo,
    };
  }

  return filters;
}

export function tryFiltersFromSearchParams(p: URLSearchParams): {
  filters: OrderFilters;
  error?: string;
} {
  try {
    return { filters: filtersFromSearchParams(p) };
  } catch (error) {
    return {
      filters: {},
      error: error instanceof Error ? error.message : "日期筛选参数无效",
    };
  }
}

export function filtersToSearchParams(filters: OrderFilters): URLSearchParams {
  const p = new URLSearchParams();
  (["status", "channel", "keyword", "date_basis", "date_from", "date_to"] as const).forEach(
    (key) => {
      const value = filters[key];
      if (value) p.set(key, value);
    },
  );
  return p;
}
