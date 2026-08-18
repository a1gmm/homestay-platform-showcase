import type { Dayjs } from "dayjs";
import type { OrderCreate, OrderRoomCreate } from "./types";

/**
 * 新建订单表单里「一行房间」的形状（Form.List name="rooms" 的每一项）。
 * /orders/new 完整页与甘特图 QuickCreateOrderModal 共用此结构。
 */
export type RoomFormRow = {
  room_id?: string | null;
  dates?: [Dayjs | null, Dayjs | null] | null;
  list_price?: number | null;
  actual_price?: number | null;
  guests_count?: number;
  // 每房净房费（B 方案 2026-07-04）：多房平台单手填。铁律：任一行填 ⇒ 全填 +
  // Σ = 整单净房费（提交端负责派生整单值，后端 422 兜底）。
  ota_owner_revenue?: number | null;
};

/**
 * 由某行的入住/退房日期算出住了几晚（退房 - 入住）。纯展示用，不可编辑。
 * 缺任一日期、或退房未晚于入住（≤0 晚）时返回 null，交由调用方决定不渲染。
 * 续住/改期只需改日期，晚数随之重算，无需手填。
 */
export function roomNights(
  dates: [Dayjs | null, Dayjs | null] | null | undefined
): number | null {
  const checkIn = dates?.[0];
  const checkOut = dates?.[1];
  if (!checkIn || !checkOut) return null;
  const nights = checkOut.diff(checkIn, "day");
  return nights > 0 ? nights : null;
}

/**
 * 把表单的多行房间转成后端 OrderRoomCreate[]，并算出订单合计实收。
 * - 丢弃没填日期的行
 * - 空 room_id → null（待排房）；缺失价格 → null；人数缺省 0
 * - position 按可用行顺序递增
 */
export function roomFormRowsToPayload(rows: RoomFormRow[] | undefined): {
  rooms: OrderRoomCreate[];
  totalActual: number;
} {
  const rooms: OrderRoomCreate[] = (rows || [])
    .filter((r) => r?.dates?.[0] && r?.dates?.[1])
    .map((r, i) => ({
      room_id: r.room_id || null,
      check_in_date: r.dates![0]!.format("YYYY-MM-DD"),
      check_out_date: r.dates![1]!.format("YYYY-MM-DD"),
      list_price: r.list_price ?? null,
      actual_price: r.actual_price ?? null,
      guests_count: r.guests_count ?? 0,
      position: i,
      ota_owner_revenue: r.ota_owner_revenue ?? null,
    }));
  const totalActual = rooms.reduce((acc, r) => acc + (r.actual_price ?? 0), 0);
  return { rooms, totalActual };
}

/**
 * 编辑订单时禁止「加房」把单房单变多房单（配合 auto-split：一间一单）。
 * 新建走拆单，编辑不拆——多房单无法表达「一间已退一间在住」，且拆一张已存在
 * (可能已收款/在住)的老单风险高。所以编辑里增房一律拦下，引导去新建订单。
 *
 * @param originalCount 打开编辑弹窗时订单原有的房间数
 * @param submittedCount 提交时表单里的房间数
 * @returns 违规文案（房数变多）；合规返回 null（不变或减少都放行，允许改/删/换房）
 */
export function editRoomAddBlockMessage(
  originalCount: number,
  submittedCount: number
): string | null {
  if (submittedCount > originalCount) {
    return "一间一单：编辑订单不能加房。如需为该客人增加房间，请「新建订单」（系统会各自成单，退房互不影响）。";
  }
  return null;
}

/**
 * 押金按房间数平摊到「分」；除不尽的零头归第一间，保证合计守恒。
 * splitDeposit(201, 2) → [100.5, 100.5]；splitDeposit(100, 3) → [33.34, 33.33, 33.33]。
 */
export function splitDeposit(total: number, n: number): number[] {
  if (n <= 1) return [total];
  const cents = Math.round((total || 0) * 100);
  const base = Math.floor(cents / n);
  const rem = cents - base * n;
  return Array.from({ length: n }, (_, i) => (base + (i === 0 ? rem : 0)) / 100);
}

/**
 * 「同客多间自动拆单」：把一张多房 payload 拆成 N 张单房 payload，各建一张独立订单。
 * 王总诉求：一个人订多间，从根上分成多张单，退一间不影响另一间（根因：OrderRoom 无
 * 独立状态字段，同单退房按整单流转会连带）。只在前台手动建单时拆；OTA 同步/C 端预订
 * 走各自后端链路、不经此函数，天然不受影响。
 *
 * 规则：
 * - 只有 1 间（或空）→ 原样返回 [payload]，行为完全不变。
 * - ≥2 间 → 每间各出一张单：
 *   - 订单级房费(actual_price) = 该房行房费（不再是整单合计）；挂牌/优惠由后端从
 *     单房行回填，不显式带。
 *   - 押金按房间数平摊（零头归第一间）。
 *   - 每房净房费(ota_owner_revenue) 只带该房行自己的；整单级聚合值不套用，避免多张单
 *     重复计入。
 *   - 第 2 张起是「有意的兄弟单」，带 allow_duplicate 跳过「同客同日期重复单」拦截。
 */
export function splitOrderPayloadByRoom(payload: OrderCreate): OrderCreate[] {
  const rooms = payload.rooms ?? [];
  if (rooms.length <= 1) return [payload];
  const deposits = splitDeposit(payload.deposit ?? 0, rooms.length);
  return rooms.map((room, i) => ({
    ...payload,
    rooms: [{ ...room, position: 0 }],
    actual_price: room.actual_price ?? null,
    deposit: deposits[i],
    ota_owner_revenue: room.ota_owner_revenue ?? undefined,
    allow_duplicate: i > 0 ? true : payload.allow_duplicate,
  }));
}

/**
 * 汇总多行房间的「每房净房费」手填状态（B 方案 2026-07-04）。
 * - anyNet：至少一行填了（非平台渠道调用方传 isPlatform=false，恒 false）
 * - allNet：每一行都填了（anyNet && !allNet ⇒ 提交前拦「要么全填要么全不填」）
 * - sum：全填时的 Σ（截到分），作为整单净房费提交；未全填为 null
 */
export function summarizePerRoomNets(
  rows: Array<{ ota_owner_revenue?: number | null } | undefined> | undefined,
  isPlatform: boolean
): { anyNet: boolean; allNet: boolean; sum: number | null } {
  const nets = (rows || []).map((r) => (isPlatform ? r?.ota_owner_revenue : null));
  const anyNet = nets.some((v) => v != null);
  const allNet = nets.length > 0 && nets.every((v) => v != null);
  const sum =
    anyNet && allNet
      ? Number(nets.reduce((acc: number, v) => acc + Number(v), 0).toFixed(2))
      : null;
  return { anyNet, allNet, sum };
}

/**
 * 每房净房费仅在「平台渠道 + 多于 1 间房」时生效（B 方案 2026-07-04）。
 * 单房单每房净值 == 整单净值、无意义；非平台单无佣金概念。两个录单入口 + 编辑弹窗
 * 统一用此判定，避免删房到 1 间 / 切换非平台渠道时残留的每房值静默生效（评审 2026-07-05）。
 */
export function perRoomNetActive(isPlatform: boolean, roomCount: number): boolean {
  return isPlatform && roomCount > 1;
}

/**
 * 某房净房费的比例分摊占位值 = 整单净房费 × 本房房费 / 房费合计（截到分）。
 * 映射结算引擎的回退口径；缺整单净值/本房房费/合计≤0 时返回 null。
 * 两个录单入口的每房净房费输入共用，避免占位公式分叉。
 */
export function prorateOrderNet(
  orderNet: number | null | undefined,
  rowFee: number | null | undefined,
  totalFee: number,
): string | null {
  if (orderNet == null || rowFee == null || !(totalFee > 0)) return null;
  return ((Number(orderNet) * Number(rowFee)) / totalFee).toFixed(2);
}

/**
 * 排房/换房弹窗的「当前房」解析。
 *
 * 指定了行（pickedOrderRoomId）时以该行为唯一真相：待排房行(room_id=null)的
 * 当前房就是「无」，绝不能回退到订单顶层 room_id——顶层是首行的房，回退会把
 * 首行已排的房当成本行可选项、绕过按日期段的可订过滤；该房若当晚已被别的订单
 * 占用，前台选中确认必然 409（2026-07-04 关联房单 ORD-20260701-3C01 实锤）。
 * 指定行 id 不在订单里（陈旧 id）同理按「无当前房」处理。
 *
 * 未指定行 = 旧单房入口，保持原兼容行为：首行的房，缺了再回退顶层 room_id。
 */
export function resolveAssignCurrentRoom(
  rooms: Array<{ order_room_id?: string; room_id?: string | null }> | undefined,
  pickedOrderRoomId: string | undefined,
  orderTopRoomId: string | null | undefined
): string | undefined {
  if (pickedOrderRoomId) {
    const row = (rooms ?? []).find((r) => r.order_room_id === pickedOrderRoomId);
    return row?.room_id ?? undefined;
  }
  return (rooms ?? [])[0]?.room_id ?? orderTopRoomId ?? undefined;
}

interface FormLike {
  getFieldValue: (path: (string | number)[]) => unknown;
  setFieldValue: (path: (string | number)[], value: unknown) => void;
}

/**
 * issue#103 Step 3：某行填了实收且该行「客人价」为空时，自动把实收带入客人价。
 * 只动用户实际改动的行（onValuesChange 的 changed.rooms 是稀疏数组），不覆盖已填值。
 */
export function syncRoomRowsListPrice(
  changedRooms: Array<{ actual_price?: number | null } | undefined> | undefined,
  form: FormLike
): void {
  if (!changedRooms) return;
  changedRooms.forEach((rc, idx) => {
    if (rc && rc.actual_price != null) {
      const cur = form.getFieldValue(["rooms", idx, "list_price"]);
      if (cur == null || cur === "") {
        form.setFieldValue(["rooms", idx, "list_price"], rc.actual_price);
      }
    }
  });
}
