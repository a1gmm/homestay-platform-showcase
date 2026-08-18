import type { QueryClient } from "@tanstack/react-query";

/**
 * 订单 mutation（状态流转 / 取消）后统一失效的 query keys。
 *
 * 关键：必须含 `["order"]`。OrderDetailModal 抽屉的「下一步」按钮读的是
 * `useLiveOrder` → `["order", id]` 这条 live query；`["orders"]`（列表）不是它的前缀，
 * 漏了 `["order"]` 抽屉就不会重新拉取 → 按钮停在旧状态 → 用户被迫手动刷新（「一步一刷新」），
 * 甚至对旧按钮再点一次触发后端无效流转报错（「状态不允许从 X 到 X」）。
 *
 * 同一个坑 #50 已为「收款」单独补过 `["order"]`（见 hooks/useOrders.ts 注释），
 * 本函数把 transition / cancel 两处共用入口一并收口，避免再次漏改。
 */
export function invalidateOrderRelated(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: ["orders"] });
  qc.invalidateQueries({ queryKey: ["order"] });
  // 续住段整段视图（详情抽屉的整段头部/分段明细/「看着像续住」提示条）。
  // 同上面那个坑：["stay-group", id] 不是 ["order"] / ["orders"] 的前缀，漏了它
  // 关联续住成功后提示条不消失、分段明细不出现，前台以为没关上、再点一次得到后端 400。
  qc.invalidateQueries({ queryKey: ["stay-group"] });
  qc.invalidateQueries({ queryKey: ["rooms"] });
  qc.invalidateQueries({ queryKey: ["staff", "calendar"] });
  // Dashboard 今日清单卡/月度指标（staleTime 2min）：漏了它，运营处理完订单
  // 回首页看到的还是旧数字，最长 2 分钟——足够让人不信这张卡。
  qc.invalidateQueries({ queryKey: ["dashboard"] });
}

/**
 * 收款（记录收款）mutation 后统一失效的 query keys。
 *
 * 和 invalidateOrderRelated 的失效集合不同：含 `["payments"]`（收款记录列表），
 * 不含 `["rooms"]` / `["staff","calendar"]`（收款不改房态、不影响排班）。
 *
 * 同样必须含 `["order"]`：订单页 / 房态页（运营台）的详情抽屉读 `["order", id]` live query，
 * `["orders"]`（列表）不是它的前缀，漏了 `["order"]` 抽屉的「收款记录 / 已收金额 /
 * 完成订单按钮的收齐校验」就不会刷新 → 用户被迫手动刷新。
 * #50 曾在 hooks/useOrders.ts 内联补过 `["order"]`，但 rooms/page.tsx 那份漏网；
 * 本函数把两处共用入口收口，避免再次漏改。
 */
export function invalidatePaymentRelated(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: ["orders"] });
  qc.invalidateQueries({ queryKey: ["payments"] });
  qc.invalidateQueries({ queryKey: ["order"] });
  // 收退押金会改整段视图的押金口径（group_view → deposit_holder），同样要失效段视图。
  qc.invalidateQueries({ queryKey: ["stay-group"] });
  // 收款直接影响 Dashboard 的今日/月度收入指标，同样需要即时刷新。
  qc.invalidateQueries({ queryKey: ["dashboard"] });
}

/**
 * 订单变更（改单/排房/换房/改日期/改价/撤销退房…）+ 审计时间线 组合失效。
 *
 * OrderDetailModal 里 10 个 mutation 此前各手写 3-4 条 invalidate（39 处），
 * 新增数据源（如 dashboard 卡）要人肉扫全部 onSuccess，漏一处就是「不刷新」bug
 * （#50 已重复踩过两次）。收口到本函数：订单相关全集 + 该单的审计时间线。
 */
export function invalidateOrderWithAudit(qc: QueryClient, orderId?: string): void {
  invalidateOrderRelated(qc);
  qc.invalidateQueries({ queryKey: ["payments"] });
  if (orderId) qc.invalidateQueries({ queryKey: ["audit-logs", "order", orderId] });
}

/** 收款变更（记录/修改/删除收款、收退押金）+ 审计时间线 组合失效。 */
export function invalidatePaymentWithAudit(qc: QueryClient, orderId?: string): void {
  invalidatePaymentRelated(qc);
  if (orderId) qc.invalidateQueries({ queryKey: ["audit-logs", "order", orderId] });
}
