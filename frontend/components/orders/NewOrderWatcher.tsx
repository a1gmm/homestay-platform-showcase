"use client";

import React, { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, notification } from "antd";
import { useRouter } from "next/navigation";
import { ordersApi } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";
import { useNewOrderStore } from "@/lib/new-order-store";
import { playNewOrderDing, primeAudioOnFirstGesture } from "@/lib/new-order-sound";
import {
  shouldWatchNewOrders,
  diffNewOrders,
  formatOrderToast,
  formatMergedToast,
  MERGE_THRESHOLD,
} from "@/lib/new-order-alert";
import type { OrderListItem } from "@/lib/types";

/**
 * 新订单到店提示。挂在 (dashboard)/layout 鉴权闸之内。
 *
 * 为什么是轮询不是推送：bypms 同步引擎 60s 才拉一轮才建单，端到端延迟大头在
 * 携程→bypms 那一侧，前端做实时推送没有收益。
 *
 * 为什么按 id 集合比对而不是 created_at 水位线：PG 的 now() 是事务开始时间，
 * 慢事务会写入「过去的」时间戳，水位线会永远跳过它且无人知晓。
 *
 *                          每 30s 一轮
 *                               │
 *                               ▼
 *                    ┌──────────────────────┐
 *                    │ role === "operator"? │
 *                    └──────────┬───────────┘
 *                     否 ◀──────┴──────▶ 是
 *                     │                  │
 *            渲染 null / 不起 query        ▼
 *            (admin 王总/finance    GET /orders?page_size=20
 *             都不该被订单叮)        key=["new-order-watch"] ①
 *                                        │
 *                                        ▼
 *                             ┌────────────────────┐
 *                             │  baselineTaken ?   │
 *                             └─────────┬──────────┘
 *                              否 ◀─────┴─────▶ 是
 *                              │                │
 *                    takeBaseline(items)        ▼
 *                    一声不响 ②          diffNewOrders(seen, items)
 *                    (刷新页面不弹历史单)        │
 *                                               ▼
 *                                          fresh.length?
 *                                          ┌────┴────┐
 *                                         =0        >0
 *                                          │         │
 *                                        返回        ▼
 *                                          ┌─────────┴─────────┐
 *                                        1~3                  >3
 *                                          │                   │
 *                                     逐条 toast          合并成一条 ③
 *                                          └─────────┬─────────┘
 *                                                    ▼
 *                                              markSeen(fresh) ⑤
 *                                                    │
 *                                                    ▼
 *                                          muted ? 静默 : 响一声
 *                                          (一轮只响一声)
 *
 * ⚠️ 图上标号 = 五个「看不出来但改了就炸」的约束，动它们之前先读设计文档：
 *   ① query key 不能进 ["orders"] 前缀 —— invalidateOrderRelated（见 lib/order-cache.ts）
 *      会前缀失效它，导致每次订单改动都重拉，并和 markSeen 抢时序。
 *   ② 首次只取基线不弹窗 —— 去掉这条，前台每次刷新被连弹 20 次。
 *   ③ >3 合并 —— 去掉这条，断网恢复后连击。注意是「>」不是「>=」：恰好 3 单仍逐条弹。
 *   ④ (见 lib/new-order-alert.ts) 裁剪上限 200 且按 created_at 最旧淘汰 ——
 *      设成 ≤page_size(20) 会让第一页上的老单被淘汰后重新弹，制造随机幽灵响声。
 *   ⑤ markSeen 必须在弹窗**之后** —— 放前面的话，弹窗一抛异常这些单就被永久标记
 *      已知，前台永远等不到提示。（两种顺序都不会重复弹：effect 同步跑完才重渲染，
 *      重跑时 diff 已为空。所以选能扛异常的那个。）
 *   另见 lib/create-orders.ts：建单时 markSeen 必须在循环内逐单调，等 onSuccess
 *   会让多房单被轮询撞上，弹出前台自己录的单。
 */
export default function NewOrderWatcher() {
  const router = useRouter();
  // 用选择器只订阅 role：useAuthStore() 不带选择器会订阅整个 store，
  // 任何 auth 写入（setAuth / token 轮换）都会重渲染并重跑下面的 toast effect。
  const role = useAuthStore((s) => s.user?.role);
  // maxCount: 3 —— toast 不自动消失（见下方 DURATION 注释），所以必须限量，
  // 否则一天堆二三十条要前台一条条点叉。新的自动挤掉最旧的。
  //
  // ⚠️ 已知取舍（王总 2026-07-17 拍板接受，别当 bug 修）：
  // MERGE_THRESHOLD 只在同一轮轮询内合并，而真实订单是一单一单进的（bypms 60s
  // 同步、本处 30s 轮询），所以现实中几乎每条都是独立 toast，合并基本不触发。
  // 于是前台在飞书里一小时进了 5 单 → 屏上只留最新 3 条，早的 2 条被 antd 挤掉，
  // 且因为已在 seen 里而**永不重弹**——她不会知道还有两单。
  // 为什么可以接受：单子没丢，就在订单页和待排房栏里站着，而待排房栏本次已改成
  // 自动刷新（rooms/page.tsx），那才是真正的待办清单。toast 只负责「叫一声」，
  // 不负责当清单。要改的话是加「另有 N 条」折叠，不是调大 maxCount。
  const [api, contextHolder] = notification.useNotification({ maxCount: 3 });
  const enabled = shouldWatchNewOrders(role);

  const seen = useNewOrderStore((s) => s.seen);
  const baselineTaken = useNewOrderStore((s) => s.baselineTaken);
  const muted = useNewOrderStore((s) => s.muted);
  const markSeen = useNewOrderStore((s) => s.markSeen);
  const takeBaseline = useNewOrderStore((s) => s.takeBaseline);
  const setMuted = useNewOrderStore((s) => s.setMuted);

  // 首次用户手势时预热音频。不做的话前台整段没声音：登录走跨域交接
  // （www 域 → admin 域的 /auth/accept 页），落到 admin 域时她从没点过任何东西，
  // 粘性激活不跨域名继承 → AudioContext 永远 suspended。而本功能的设定场景正是
  // 「她开着页面、人在飞书」，也就是她真的不会主动去点。
  useEffect(() => {
    if (!enabled) return;
    return primeAudioOnFirstGesture();
  }, [enabled]);

  // ⚠️ query key 必须独立，不能放 ["orders"] 前缀下——invalidateOrderRelated
  // (lib/order-cache.ts:15) 会前缀失效它，导致每次订单改动都触发重拉。
  const { data } = useQuery({
    queryKey: ["new-order-watch"],
    queryFn: () => ordersApi.list({ page_size: 20 }).then((r) => r.data),
    enabled,
    refetchInterval: 30_000,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
    // 前台整天在飞书↔浏览器之间切。后台轮询一直在跑、数据从不会旧，所以每次
    // 聚焦触发的重拉都是白发的。staleTime 与轮询周期取齐 → 聚焦重拉全部去重。
    staleTime: 30_000,
  });

  useEffect(() => {
    if (!enabled || !data) return;

    // ⚠️ 必须验形状而不只是 ?? []：后端若用 HTTP 200 返回错误信封，items 会是个
    // 非数组，下面的 .filter 直接抛——而这是 layout 自己抛的错，(dashboard)/error.tsx
    // 捕不到（它只管子段），会一路冒到 app/error.tsx 整屏变「系统错误」。
    const items: OrderListItem[] = Array.isArray(data.items) ? data.items : [];

    if (!baselineTaken) {
      // ⚠️ 空响应不取基线：抖动导致的空返回会把 baselineTaken 立在**空的** seen 上，
      // 下一轮 20 张真单全被判成新单，弹一条「20 个新订单」——基线存在的唯一意义
      // （刷新不弹历史单）就此报废。真的一张单都没有时也跳过，那第一张真单弹提示
      // 反而是对的。
      if (items.length === 0) return;
      takeBaseline(items); // 首次只灌基线，一声不响
      return;
    }

    const fresh = diffNewOrders(seen, items);
    if (fresh.length === 0) return;

    const goto = (orderId?: string) =>
      router.push(orderId ? `/orders?keyword=${encodeURIComponent(orderId)}` : "/orders");

    // ⚠️ stopPropagation 必须有：按钮在 toast 内部，不拦冒泡就会连带触发
    // 整条 toast 的 onClick——前台想关个声音，人被弹到订单页去了。
    //
    // ⚠️ actions 不是 btn：antd 5.29 起 `btn` 已废弃（package.json 是 ^5.25.0，实际会
    // 装到 5.29+），用 btn 会在每弹一条 toast 时往控制台打一次 deprecation error。
    const muteActions = [
      <Button
        key="mute"
        size="small"
        onClick={(e) => {
          e.stopPropagation();
          setMuted(true);
        }}
      >
        关闭提示音
      </Button>,
    ];

    // ⚠️ duration: 0 = 不自动消失。理由见 rooms/page.tsx:145-146 的注释：
    // 「前台常把房态板放在屏上、人却在飞书操作」——她大概率不在浏览器里，会自动消失的
    // toast 等她回头早没了，只剩一声响，而声音说不出「是哪一单」。
    //
    // ⚠️ 但「不消失」必须配 maxCount，否则就是灾难：MERGE_THRESHOLD 只在**同一轮轮询内**
    // 合并，而真实订单是一单一单进的——每轮 1 单弹 1 条永不消失的，一天挂二三十条，前台
    // 得一条条点叉。那比现在更不顺手，而「顺手」是这功能唯一的存在理由。
    const DURATION = 0;

    // ⚠️ 整段包 try：这里是 layout 渲染的组件，抛错会一路冒到 app/error.tsx 整屏
    // 变「系统错误」——(dashboard)/error.tsx 捕不到自己 layout 抛的错。一个畸形的
    // 订单字段不该让前台盯着错误页。
    try {
      if (fresh.length > MERGE_THRESHOLD) {
        const t = formatMergedToast(fresh.length);
        api.open({
          key: `new-orders-${fresh[0].order_id}`,
          message: t.message,
          description: t.description,
          duration: DURATION,
          actions: muteActions,
          onClick: () => goto(),
        });
      } else {
        fresh.forEach((o) => {
          const t = formatOrderToast(o);
          api.open({
            key: `new-order-${o.order_id}`,
            message: t.message,
            description: t.description,
            duration: DURATION,
            actions: muteActions,
            onClick: () => goto(o.order_id),
          });
        });
      }
    } catch {
      // 弹窗失败就不标记，下一轮重试。宁可重弹也不要「永久标记已读却什么都没显示」。
      return;
    }

    // ⚠️ markSeen 必须在弹窗**之后**：放前面的话，弹窗一抛异常这些单就被永久标记
    // 已知了——前台永远等不到它们的提示，而单子确实进来了。放后面则失败可重试。
    // 两种顺序都不会重复弹（effect 同步跑完才重渲染，重跑时 diff 已为空）。
    markSeen(fresh);

    if (!muted) playNewOrderDing(); // 一轮只响一声，哪怕弹了多条
  }, [enabled, data, baselineTaken, seen, markSeen, takeBaseline, muted, setMuted, api, router]);

  if (!enabled) return null;
  return <>{contextHolder}</>;
}
