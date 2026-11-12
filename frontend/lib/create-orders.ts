import { ordersApi } from "@/lib/api";
import type { SeenItem } from "@/lib/new-order-alert";
import type { OrderCreate } from "@/lib/types";
import { extractErrorMessage, extractConflictOrderId } from "@/lib/api-errors";

export class PartialOrderCreationError extends Error {
  readonly response: { status: number; data: { detail: string }; headers: Record<string, string> };
  constructor(readonly createdIds: string[], cause: unknown, readonly remainingPayloads: OrderCreate[]) {
    const detail = cause instanceof Error && !("isAxiosError" in cause) && !("response" in cause)
      ? cause.message : extractErrorMessage(cause);
    const message = `已创建 ${createdIds.length} 张订单，表单中仅保留未创建的房间。${detail}`;
    super(message, { cause });
    const conflictId = extractConflictOrderId(cause);
    this.response = { status: 409, data: { detail: message },
      headers: conflictId ? { "x-conflict-order-id": conflictId } : {} };
  }
}

export type ResolveCreationDecision = (error: unknown, payload: OrderCreate) => Promise<OrderCreate | null>;

/**
 * 逐张建单，并在**循环内**把每张刚建好的单标记为「已知」，返回新建的 order_id 列表。
 *
 * 为什么 markSeen 必须在循环内、不能等 mutation 的 onSuccess：
 * onSuccess 要等 N 次 await create 全跑完才触发。三间房的单要建好几秒，若 30s 的
 * 新订单轮询恰好在循环中间醒来，第 1 张单**已提交、已可见、但还没进 seen** →
 * 就会弹一条「新订单」给刚刚亲手录完它的前台。必现，且恰好是误报。
 *
 * ⚠️ 诚实说明：这**缩小**了竞态窗口，没有消灭它。订单在后端 commit 的那一刻就对
 * GET /orders 可见了，而 markSeen 要等 HTTP 响应回到客户端才执行——中间约
 * 100-300ms 是裸的。轮询正好落在这个窗口里，前台仍会被自己刚录的单弹一次
 * （每单约 0.7% 概率）。接受这个残留：代价是纯观感，而彻底消灭要么得让后端
 * 回传可预测的 order_id、要么得加「正在建单」的时间窗过滤，都不值这个价。
 * 真正被消灭的是「多房单建好几秒」那个大窗口（几秒 → 几百毫秒）。
 *
 * 为什么抽成共享函数：建单有两个入口（orders/new/page.tsx 与 QuickCreateOrderModal），
 * 逻辑重复且都没测试；抽出来两边各调一行，且这段时序能被独立测到。
 *
 * 不吞异常：建单失败要让调用方的 mutation 走 onError 显示错误。已建好的那些单
 * 仍然保持已标记状态——它们真的建出来了，不标就会弹给前台自己。
 */
export async function createOrdersAndMarkSeen(
  payloads: OrderCreate[],
  markSeen: (items: SeenItem[]) => void,
  resolveDecision?: ResolveCreationDecision,
): Promise<string[]> {
  const ids: string[] = [];
  for (const p of payloads) {
    let current = p;
    try {
      // A confirmation retries only this uncreated order, never earlier rooms.
      for (let attempt = 0; ; attempt++) {
        try {
          const r = await ordersApi.create(current);
          markSeen([{ order_id: r.data.order_id, created_at: r.data.created_at }]);
          ids.push(r.data.order_id);
          break;
        } catch (error) {
          if (!resolveDecision || attempt >= 4) throw error;
          const next = await resolveDecision(error, current);
          if (!next) throw error;
          current = next;
        }
      }
    } catch (error) {
      if (ids.length) throw new PartialOrderCreationError(ids, error, payloads.slice(ids.length));
      throw error;
    }
  }
  return ids;
}
