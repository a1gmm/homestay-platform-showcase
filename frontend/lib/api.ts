import axios, { AxiosError, InternalAxiosRequestConfig } from "axios";
import type {
  OrderCreate,
  OrderOut,
  OrderListItem,
  PaginatedOrderList,
  PaginatedSegmentList,
  StayGroup,
  DashboardOverview,
  RoomOut,
  RoomCreate,
  RoomUpdate,
  PaymentCreate,
  PaymentOut,
  RefundCreate,
  RefundOut,
  ExpenseCreate,
  ExpenseOut,
  ByRoomSummaryItem,
  RoomRevenueSegment,
  OwnerOut,
  OwnerCreate,
  SettlementDetail,
  TaskCreate,
  TaskUpdate,
  TaskOut,
  DashboardToday,
  DashboardMonthly,
  RevenueTrendItem,
  CalendarRoom,
  LoginResponse,
  User,
  UserCreate,
  RoomPricingPoint,
  OrderStatus,
  GuestOut,
  GuestUpdate,
  GuestOrderHistory,
  GuestSummary,
  RoomBlockOut,
  RoomBlockCreate,
  OwnerSettlementOut,
  NotificationOut,
  NotificationTemplateOut,
  CheckinGuideRequest,
  CheckinGuideResponse,
  ChannelAnalysisItem,
  ComparisonReport,
  DepositActionResponse,
  SearchResult,
  HostingLeadCreate,
  HostingLeadOut,
  OrderManualControl,
  SourcePriceSnapshot,
  SourcePriceSnapshotOverridePayload,
  ZeroFeeSplitPayload,
  ZeroFeeSplitResult,
  ManualOverrideField,
} from "./types";
import type {
  DiffActionResult,
  ReconBatchOut,
  ReconDiffOut,
} from "./billing-recon";

export const api = axios.create({
  baseURL: process.env.NEXT_PUBLIC_API_URL || "/api/v1",
  withCredentials: true,
  // Bound the wait so a stuck request can't pin a button forever. Set generously
  // so a one-off Neon cold start (worst-case ~18s) still completes; anything
  // longer is almost certainly a real outage.
  timeout: 25000,
});

// billing-recon 上传要跑 AI 列映射 + 对账，可能要几十秒，不能复用上面的默认 25s
// （否则正常长任务会被误判超时）；诊断见 billingReconApi.upload。
export const BILLING_RECON_UPLOAD_TIMEOUT_MS = 180000;

// 错误文案归一逻辑移到 lib/api-errors.ts（owner/staff/booking 子端共用），
// 这里重导出保持既有 import 不变。
export { extractErrorMessage, isDuplicateOrderError } from "./api-errors";

// Attach access token from localStorage (fallback for non-cookie auth)
api.interceptors.request.use((config) => {
  if (typeof window !== "undefined") {
    const token = localStorage.getItem("access_token");
    if (token) config.headers["Authorization"] = `Bearer ${token}`;
  }
  return config;
});

// Auto-refresh on 401 — single-flight so concurrent 401s don't stampede /auth/refresh.
// If refresh rotates tokens server-side, parallel calls would race and invalidate each other.
let refreshInflight: Promise<string> | null = null;

async function performRefresh(): Promise<string> {
  const refresh_token = localStorage.getItem("refresh_token");
  if (!refresh_token) throw new Error("No refresh token");
  const { data } = await axios.post<{ access_token: string; refresh_token: string }>(
    "/api/v1/auth/refresh",
    { refresh_token }
  );
  localStorage.setItem("access_token", data.access_token);
  localStorage.setItem("refresh_token", data.refresh_token);
  return data.access_token;
}

function clearAuthAndRedirect() {
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
  localStorage.removeItem("last_activity_at");
  // 已在 /login 时不再整页跳转,避免登录失败时无谓刷新、吞掉表单报错 (#43)。
  if (typeof window !== "undefined" && window.location.pathname !== "/login") {
    window.location.href = "/login";
  }
}

// 登录/刷新/登出/统一登录等认证端点的 401 是"凭证错误",不是"access token 过期"。
// 这类 401 必须原样抛回给调用方,不能走刷新+跳转,否则错误密码会被当成会话过期、
// 触发整页跳转并吞掉登录表单的报错 (#43)。
const AUTH_ENDPOINT_RE =
  /\/auth\/(login|refresh|logout|unified|me\/password)|\/staff\/auth\/|\/owner\/auth\//;

// 公开端点(无需登录,如 tuoguan 落地页留资)。落地页访客没有 token,这类请求的 401
// 不能触发 refresh/跳 /login——必须原样抛回给调用方,否则访客会被踹到登录页。
const PUBLIC_ENDPOINT_RE = /\/hosting-leads($|\?|\/)/;

api.interceptors.response.use(
  (res) => res,
  async (error: AxiosError) => {
    const original = error.config as InternalAxiosRequestConfig & { _retry?: boolean };
    const isAuthEndpoint = !!original?.url && AUTH_ENDPOINT_RE.test(original.url);
    const isPublicEndpoint = !!original?.url && PUBLIC_ENDPOINT_RE.test(original.url);
    if (
      error.response?.status === 401 &&
      original &&
      !original._retry &&
      !isAuthEndpoint &&
      !isPublicEndpoint
    ) {
      original._retry = true;

      // 跨标签页竞态:另一标签可能已刷新出新 token。若 localStorage 里的 access token
      // 已不同于本请求所用的,直接用新 token 重放,不再触发刷新(避免重复轮换互相作废)(#50)。
      const usedAuth = original.headers?.["Authorization"];
      const stored = typeof window !== "undefined" ? localStorage.getItem("access_token") : null;
      if (stored && usedAuth && `Bearer ${stored}` !== usedAuth) {
        original.headers["Authorization"] = `Bearer ${stored}`;
        return api(original);
      }

      try {
        // Coalesce: only the first caller triggers the network request; others await the same promise.
        if (!refreshInflight) {
          refreshInflight = performRefresh().finally(() => {
            refreshInflight = null;
          });
        }
        const newAccessToken = await refreshInflight;
        original.headers["Authorization"] = `Bearer ${newAccessToken}`;
        return api(original);
      } catch {
        // 刷新失败:可能是另一标签刚轮换过(本标签的 refresh token 已作废)。
        // 再读一次 localStorage,若已有新 access token 就重放,否则才登出 (#50)。
        const fresh = typeof window !== "undefined" ? localStorage.getItem("access_token") : null;
        if (fresh && usedAuth && `Bearer ${fresh}` !== usedAuth) {
          original.headers["Authorization"] = `Bearer ${fresh}`;
          return api(original);
        }
        clearAuthAndRedirect();
      }
    }
    return Promise.reject(error);
  }
);

// ─── Auth ────────────────────────────────────────────────────────────────────
export const authApi = {
  login: (username: string, password: string) =>
    api.post<LoginResponse>("/auth/login", { username, password }),
  logout: () => api.post("/auth/logout"),
  me: () => api.get<User>("/auth/me"),
  changePassword: (current_password: string, new_password: string) =>
    api.post<{ message: string }>("/auth/me/password", { current_password, new_password }),
};

// 跨子域登录:一次性交接码 (#46)。token 不再放进跳转 URL。
export interface HandoffPayload {
  at: string;
  rt?: string;
  kind?: string;
  uid?: string;
  role?: string;
  name?: string;
  next?: string;
}
export const handoffApi = {
  create: (payload: HandoffPayload) =>
    api.post<{ code: string }>("/auth/handoff/create", payload),
  exchange: (code: string) =>
    api.post<HandoffPayload>("/auth/handoff/exchange", { code }),
};

// 统一登录: 手机号 + OTP 识别 user / owner / customer 任一身份
export interface UnifiedIdentity {
  kind: "user" | "owner" | "customer";
  access_token: string;
  redirect_to: string;
  role?: string;
  user_id?: string;
  display_name?: string;
  refresh_token?: string;
  owner_id?: string;
  name?: string;
  label?: string;
  subtitle?: string;
}

export const unifiedAuthApi = {
  sendOtp: (phone: string) =>
    api.post<{ sent: boolean; matched: boolean }>("/auth/unified-send-otp", { phone }),
  verify: (phone: string, code: string) =>
    api.post<{ phone: string; identities: UnifiedIdentity[] }>(
      "/auth/unified-verify",
      { phone, code }
    ),
};

export const usersApi = {
  list: () => api.get<User[]>("/auth/users"),
  create: (data: UserCreate) => api.post<User>("/auth/users", data),
  updateStatus: (user_id: string, is_active: boolean) =>
    api.patch<User>(`/auth/users/${user_id}`, { is_active }),
  resetPassword: (user_id: string, new_password: string) =>
    api.post(`/auth/users/${user_id}/reset-password`, { new_password }),
};

// ─── Orders ──────────────────────────────────────────────────────────────────
export const ordersApi = {
  list: (params?: Record<string, string | number | undefined>) =>
    api.get<PaginatedOrderList>("/orders", { params }),
  // 订单列表，按「段」分页（续住组算一行）。与 list 并存 —— list 按单，供 KPI 下钻和新订单提示用。
  // 返回的 items 是 StayGroup 不是订单，合计已由后端算好，别再归并。
  // ⚠️ 不要加 .then((r) => r.data)：本函数是 list 的原地替代品，签名必须与 list 一致
  //（拆包在调用点 useOrders.ts 做）。加了就是双重拆包 → 列表整片空白且不报错。
  listBySegment: (params?: Record<string, string | number | undefined>) =>
    api.get<PaginatedSegmentList>("/orders/by-segment", { params }),
  get: (id: string) => api.get<OrderOut>(`/orders/${id}`),
  create: (data: OrderCreate) => api.post<OrderOut>("/orders", data),
  update: (id: string, data: Partial<OrderCreate>) => api.patch<OrderOut>(`/orders/${id}`, data),
  cancel: (id: string) => api.post(`/orders/${id}/cancel`),
  transition: (id: string, target_status: OrderStatus) =>
    api.post(`/orders/${id}/transition`, null, { params: { target_status } }),
  // 办理入住（原子）：复用 staff_portal.handle_checkin —— 收押金 + 流转 checked_in + 下门锁码
  // 都在一次后端请求里完成。取代旧前端「先 deposit.collect 再 transition」两步非原子调用
  // （批3 item2：收押金成功但流转失败会留下押金已收/订单未入住的错位）。
  // order_room_id 选填（单间入住，镜像单间退房）：传了只入住这一间房，其余未入住房保持待入住；
  // 不传 = 整单入住（所有已排房、未入住的房一起入住，向后兼容）。押金按整单首间入住时收一次。
  handleCheckin: (id: string, collect_deposit = true, notes?: string, order_room_id?: string) =>
    api.post<{ order_id: string; order_status: string; lock_codes?: unknown }>(
      `/staff/orders/${id}/handle-checkin`,
      { collect_deposit, notes, order_room_id: order_room_id || null }
    ),
  // 「发起退房」专用：复用 staff_portal.handle_checkout，会把房态推到 pending_clean。
  // cleaner_id 选填（批3 item1「暂不派单」）：留空 = 建未分配清扫任务进待派池，稍后再派。
  // order_room_id 选填（单间退房）：传了只退这一间房，订单还有在住房时保持在住；
  // 不传 = 整单退房（把所有在住房一起退，向后兼容）。
  handleCheckout: (id: string, cleaner_id?: string, notes?: string, order_room_id?: string) =>
    api.post<{ order_id: string; order_status: string; created_task_id?: string | null }>(
      `/staff/orders/${id}/handle-checkout`,
      { cleaner_id: cleaner_id || null, notes, order_room_id: order_room_id || null }
    ),
  // 撤销退房: completed → checked_in，cancel 关联清扫任务，房态尝试恢复 occupied。
  revertCheckout: (id: string, reason: string) =>
    api.post<{
      order_id: string;
      order_status: string;
      cancelled_task_ids: string[];
      room_restored: boolean;
    }>(`/staff/orders/${id}/revert-checkout`, { reason }),
  // 撤销入住（批3 item5）: checked_in → roomed_pending_checkin，房态 occupied → reserved。误点入住用。
  revertCheckin: (id: string, reason: string) =>
    api.post<{ order_id: string; order_status: string; room_reverted: boolean }>(
      `/staff/orders/${id}/revert-checkin`,
      { reason }
    ),
  delete: (id: string) => api.delete(`/orders/${id}`),
  // 退房防呆：探测续住续单（同房同客、退房日紧接着的下一段订单）。有则退房弹窗提醒改用「门锁密码延期」。
  checkoutPrecheck: (id: string) =>
    api.get<{ continuations: { room_id: string; next_order_id: string; guest_name: string; check_in_date: string }[] }>(
      `/orders/${id}/checkout-precheck`
    ),
  // 续住门锁密码延期：把现有客人码延到新退房日，密码不变、不动订单日期/金额。
  extendLockCode: (id: string, new_checkout_date: string) =>
    api.post<{ results: { room_id: string; ok: boolean; reason: string | null }[]; new_checkout_date: string }>(
      `/orders/${id}/lock/extend-code`,
      { new_checkout_date }
    ),
  // 重发门锁密码卡到飞书密码群：只重推卡片、不碰门锁（首发被刷走/飞书抖动时用）。
  resendLockCard: (id: string) =>
    api.post<{ pushed: boolean }>(`/orders/${id}/lock/resend-card`),
  // 查看本单当前门锁密码（解密后明文）：前台在详情里直接看+复制的兜底（飞书发不出时）。
  lockCodes: (id: string) =>
    api
      .get<{
        codes: { room_id: string; room_name: string; password: string }[];
        // 空态时区分「正在下发/重试中（会自愈）」与「真没码」：入住瞬间下码可能 FAILED
        // （锁一时离线），gather 取不到但重试轮会自动重推 → issuing=true。
        issuing?: boolean;
      }>(`/orders/${id}/lock/codes`)
      .then((r) => r.data),
  // 续住关联（软关联：拴成一段连续入住，不合并不删单）
  linkCandidates: (id: string) =>
    api
      .get<{ candidates: { room_id: string; next_order_id: string; guest_name: string; check_in_date: string }[] }>(
        `/orders/${id}/link-candidates`
      )
      .then((r) => r.data.candidates),
  linkContinuation: (id: string, next_order_id: string) =>
    api.post<{ stay_group_id: string }>(`/orders/${id}/link-continuation`, { next_order_id }).then((r) => r.data),
  unlinkContinuation: (id: string) =>
    api.post<{ ok: boolean }>(`/orders/${id}/unlink-continuation`).then((r) => r.data),
  // 续住组整段视图（只读）：点任意一段都拿到同一份。无组单会返回退化的单段组 + 可能的续住候选。
  stayGroup: (id: string) => api.get<StayGroup>(`/orders/${id}/stay-group`).then((r) => r.data),
  manualControl: (id: string) =>
    api.get<OrderManualControl>(`/orders/${id}/manual-control`).then((r) => r.data),
  previewZeroFeeSplit: (id: string, payload: ZeroFeeSplitPayload) =>
    api
      .post<ZeroFeeSplitResult>(`/orders/${id}/zero-fee-split/preview`, payload)
      .then((r) => r.data),
  splitStay: (id: string, payload: ZeroFeeSplitPayload, idempotencyKey: string) =>
    api
      .post<ZeroFeeSplitResult>(`/orders/${id}/zero-fee-split`, payload, {
        headers: { "Idempotency-Key": idempotencyKey },
      })
      .then((r) => r.data),
  unlockOrderFields: (
    id: string,
    payload: { action: "unlock"; fields: ManualOverrideField[]; reason: string }
  ) => api.patch<OrderOut>(`/orders/${id}/manual-overrides`, payload).then((r) => r.data),
  createSourcePriceSnapshotOverride: (
    id: string,
    payload: SourcePriceSnapshotOverridePayload
  ) =>
    api
      .post<SourcePriceSnapshot>(
        `/orders/${id}/source-price-snapshots/admin-override`,
        payload
      )
      .then((r) => r.data),
  decideSyncConflict: (
    id: string,
    conflictId: string,
    payload: { action: "preserve" | "ignore"; reason: string }
  ) =>
    api
      .post<{ conflict_id: string; status: "open" | "ignored" }>(
        `/orders/${id}/sync-conflicts/${conflictId}/decision`,
        payload
      )
      .then((r) => r.data),
  // 运营台用：列出 room_id 为空且未取消/未完成的活跃订单
  pendingRoom: () => api.get<OrderOut[]>("/orders/pending-room"),
  // 原子排房：校验冲突 + 设 room_id + 房态联动 + 自动状态推进
  // Multi-room: order_room_id 可选，多房订单时指定要排哪一行
  assignRoom: (id: string, room_id: string, order_room_id?: string) =>
    api.post<OrderOut>(`/orders/${id}/assign-room`, { room_id, order_room_id }),
  // 换房：免费升级/房间故障/客人要求。入住中按换房日拆分 OrderRoom，加价计入新房行。
  transferRoom: (
    id: string,
    body: {
      order_room_id: string;
      new_room_id: string;
      reason: "free_upgrade" | "room_defect" | "guest_request" | "other";
      transfer_date?: string; // YYYY-MM-DD
      markup_amount?: number;
      old_room_disposition?: "maintenance" | "pending_clean" | "available";
    },
  ) => api.post<OrderOut>(`/orders/${id}/transfer-room`, body),
  // 对调房间：两个订单互换房间号（各保留日期、不改价）。返回两个更新后的订单。
  swapRooms: (body: {
    order_a_id: string;
    order_room_a_id: string;
    order_b_id: string;
    order_room_b_id: string;
  }) => api.post<OrderOut[]>(`/orders/swap-rooms`, body),
  // 单日改价：改某一晚的价格。后端重算 OrderRoom.actual_price + Order.actual_price。
  updateDailyPrice: (
    id: string,
    order_room_id: string,
    date: string,        // YYYY-MM-DD
    price: number,
  ) =>
    api.patch<OrderOut>(`/orders/${id}/rooms/${order_room_id}/daily-price`, { date, price }),
  // Feature 9: Deposit workflow
  deposit: {
    collect: (id: string) => api.post<DepositActionResponse>(`/orders/${id}/deposit/collect`),
    // 2026-06-05：退押金可填实退金额（默认全退）；少于押金时差额为扣款，须填原因。
    return: (id: string, body?: { refund_amount?: number; withhold_reason?: string }) =>
      api.post<DepositActionResponse>(`/orders/${id}/deposit/return`, body ?? {}),
    withhold: (id: string, reason: string) =>
      api.post<DepositActionResponse>(`/orders/${id}/deposit/withhold`, { reason }),
  },
};

export interface CleanerBrief {
  user_id: string;
  display_name: string;
  phone: string | null;
}

export const staffApi = {
  listCleaners: () => api.get<CleanerBrief[]>("/staff/cleaners"),
};

// ─── Rooms ────────────────────────────────────────────────────────────────────
export interface RoomImage {
  image_id: string;
  room_id: string;
  url: string;
  sort_order: number;
  is_cover: boolean;
  content_type: string | null;
  created_at: string;
}

export const roomsApi = {
  list: () => api.get<RoomOut[]>("/rooms"),
  get: (id: string) => api.get<RoomOut>(`/rooms/${id}`),
  create: (data: RoomCreate) => api.post<RoomOut>("/rooms", data),
  update: (id: string, data: RoomUpdate) => api.patch<RoomOut>(`/rooms/${id}`, data),
  delete: (id: string) => api.delete(`/rooms/${id}`),
  calendar: (year: number, month: number) =>
    api.get<CalendarRoom[]>("/rooms/availability/calendar", { params: { year, month } }),
  // 滚动窗口模式（D1）：起始日 + 天数，天然跨月。
  calendarWindow: (start: string, days: number) =>
    api.get<CalendarRoom[]>("/rooms/availability/calendar", { params: { start, days } }),
  availabilityList: (check_in: string, check_out: string, excludeOrderId?: string) =>
    api.get<{ available_room_ids: string[] }>("/rooms/availability/list", {
      params: { check_in, check_out, ...(excludeOrderId ? { exclude_order_id: excludeOrderId } : {}) },
    }),
  checkAvailability: (data: { room_id: string; check_in_date: string; check_out_date: string; exclude_order_id?: string }) =>
    api.post<{ room_id: string; available: boolean }>("/rooms/availability/check", data),
  pricing: (room_id: string, days: number = 7) =>
    api.get<RoomPricingPoint[]>(`/rooms/${room_id}/pricing`, { params: { days } }),
  pricingDetail: (room_id: string, date: string) =>
    api.get(`/rooms/${room_id}/pricing/detail`, { params: { date } }),
  triggerPricing: (room_id: string, days: number = 30) =>
    api.post(`/rooms/${room_id}/pricing/trigger`, null, { params: { days } }),
  images: {
    list: (roomId: string) => api.get<RoomImage[]>(`/rooms/${roomId}/images`),
    upload: (roomId: string, file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      return api.post<RoomImage>(`/rooms/${roomId}/images`, fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
    },
    patch: (roomId: string, imageId: string, body: { is_cover?: boolean; sort_order?: number }) =>
      api.patch<RoomImage>(`/rooms/${roomId}/images/${imageId}`, body),
    delete: (roomId: string, imageId: string) =>
      api.delete(`/rooms/${roomId}/images/${imageId}`),
    reorder: (roomId: string, orderedIds: string[]) =>
      api.post<RoomImage[]>(`/rooms/${roomId}/images/reorder`, { ordered_ids: orderedIds }),
  },
  // issue#7: 批量设置三类分成比例（admin only）
  bulkShareRatios: (room_ids: string[], ratios: { normal?: number; trial?: number; owner_self?: number }) =>
    api.post<{ updated: number; room_ids: string[] }>("/rooms/share-ratios/bulk", { room_ids, ratios }),
  // v2#2: 业主费用支出占比规则
  costShareRules: {
    list: (roomId: string) =>
      api.get<import("./cost-share-constants").CostShareRuleOut[]>(`/rooms/${roomId}/cost-share-rules`),
    upsert: (roomId: string, rules: import("./cost-share-constants").CostShareRuleItem[]) =>
      api.put<import("./cost-share-constants").CostShareRuleOut[]>(
        `/rooms/${roomId}/cost-share-rules`,
        { rules }
      ),
    bulk: (room_ids: string[], rules: import("./cost-share-constants").CostShareRuleItem[]) =>
      api.post<{ upserted: number; room_ids: string[] }>("/rooms/cost-share-rules/bulk", {
        room_ids,
        rules,
      }),
  },
};

// ─── Finance ─────────────────────────────────────────────────────────────────
export interface PaymentUpdate {
  amount?: number | string;
  method?: string;
  notes?: string | null;
}

export const financeApi = {
  payments: {
    list: (order_id?: string) => api.get<PaymentOut[]>("/payments", { params: { order_id } }),
    create: (data: PaymentCreate) => api.post<PaymentOut>("/payments", data),
    update: (payment_id: string, data: PaymentUpdate) =>
      api.patch<PaymentOut>(`/payments/${payment_id}`, data),
    remove: (payment_id: string) => api.delete(`/payments/${payment_id}`),
  },
  refunds: {
    list: (order_id?: string) => api.get<RefundOut[]>("/refunds", { params: { order_id } }),
    create: (data: RefundCreate) => api.post<RefundOut>("/refunds", data),
    // 冲正/软删退款（批2 item6）：误录退款作废，后端软删+重算 payment_status。
    remove: (refund_id: string) => api.delete(`/refunds/${refund_id}`),
  },
  expenses: {
    list: (params?: {
      room_id?: string;
      year?: number;
      month?: number;
      start_date?: string;
      end_date?: string;
    }) => api.get<ExpenseOut[]>("/expenses", { params }),
    create: (data: ExpenseCreate) => api.post<ExpenseOut>("/expenses", data),
    update: (id: string, data: ExpenseCreate) =>
      api.patch<ExpenseOut>(`/expenses/${id}`, data),
    delete: (id: string) => api.delete(`/expenses/${id}`),
    importUpload: (file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      return api.post("/expenses/import", fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
    },
    importTemplate: () =>
      api.get("/expenses/import-template", { responseType: "blob" }),
  },
  summaryByRoom: (
    params: { year?: number; month?: number; start_date?: string; end_date?: string }
  ) => api.get<ByRoomSummaryItem[]>("/summary/by-room", { params }),
  // 单房明细「收入来源」：按 OrderRoom 段返回（含多房订单里该房那一段），
  // 口径同 summaryByRoom（离店日归属 + 段级实收）。不能用 ordersApi.list({room_id})——
  // 那走 Order.room_id 顶层过滤，多房单顶层为空会漏。
  roomRevenueSegments: (
    params: { room_id: string; year?: number; month?: number; start_date?: string; end_date?: string }
  ) => api.get<RoomRevenueSegment[]>("/summary/room-segments", { params }),
  // 保洁费：续住/在住房打扫登记（金额后端固定 30，收房东）+ 误登记作废。
  // 退房打扫的 60 由后端「打扫完了」自动记，前端不涉及。
  cleaningCharges: {
    createRenewal: (data: { order_id: string; room_id: string; notes?: string }) =>
      api.post<ExpenseOut>("/cleaning-charges/renewal", data),
    void: (expense_id: string) => api.delete(`/cleaning-charges/${expense_id}`),
  },
  // 业主服务费费率（保洁/续住/洗涤/日耗）——财务页「费用标准设置」，admin only。
  serviceFeeConfig: {
    get: () => api.get<ServiceFeeConfig>("/service-fee-config"),
    update: (data: ServiceFeeConfigUpdate) =>
      api.put<ServiceFeeConfig>("/service-fee-config", data),
  },
};

export interface ServiceFeeConfig {
  checkout_cleaning_fee: number | string;
  instay_cleaning_fee: number | string;
  laundry_fee_per_room: number | string;
  consumable_fee_per_room_night: number | string;
  updated_at?: string | null;
}
export type ServiceFeeConfigUpdate = Omit<ServiceFeeConfig, "updated_at">;

// ─── Owners ──────────────────────────────────────────────────────────────────
export const ownersApi = {
  list: () => api.get<OwnerOut[]>("/owners"),
  get: (id: string) => api.get<OwnerOut>(`/owners/${id}`),
  create: (data: OwnerCreate) => api.post<OwnerOut>("/owners", data),
  update: (id: string, data: Partial<OwnerCreate>) =>
    api.patch<OwnerOut>(`/owners/${id}`, data),
  delete: (id: string) => api.delete(`/owners/${id}`),
  resetPassword: (owner_id: string, new_password: string) =>
    api.post(`/owners/${owner_id}/reset-password`, { new_password }),
  batchAssignRooms: (data: {
    room_ids: string[];
    owner_id?: string | null;
    owner_share_ratio?: number;
    owner_deduction_rules?: string[];
    owner_ignored_categories?: string[];
  }) => api.post("/owners/batch-assign-rooms", data),
};

// ─── Tasks ────────────────────────────────────────────────────────────────────
export const tasksApi = {
  list: (params?: { status?: string; order_id?: string; assignee_id?: string; overdue_only?: boolean }) =>
    api.get<TaskOut[]>("/tasks", { params }),
  create: (data: TaskCreate) => api.post<TaskOut>("/tasks", data),
  update: (id: string, data: TaskUpdate) => api.patch<TaskOut>(`/tasks/${id}`, data),
  delete: (id: string) => api.delete(`/tasks/${id}`),
  // Feature 4: Cleaning review workflow
  submit: (id: string, notes?: string) => api.post<TaskOut>(`/tasks/${id}/submit`, { notes }),
  review: (id: string, approved: boolean, rejection_reason?: string) =>
    api.post<TaskOut>(`/tasks/${id}/review`, { approved, rejection_reason }),
};

// ─── Dashboard ────────────────────────────────────────────────────────────────
export const dashboardApi = {
  overview: (year: number, month: number, months: number = 6) =>
    api.get<DashboardOverview>("/dashboard/overview", { params: { year, month, months } }),
  today: () => api.get<DashboardToday>("/dashboard/today"),
  monthly: (year: number, month: number) =>
    api.get<DashboardMonthly>("/dashboard/monthly", { params: { year, month } }),
  // 任意起止日期的财务汇总卡片（月度汇总的区间版）。收入按离店日、支出按发生日落区间。
  periodSummary: (start_date: string, end_date: string) =>
    api.get<DashboardMonthly>("/dashboard/period-summary", {
      params: { start_date, end_date },
    }),
  revenueTrend: (months?: number) =>
    api.get<RevenueTrendItem[]>("/dashboard/revenue-trend", { params: { months } }),
  // Feature 6: Channel ROI
  channelAnalysis: (year: number, month: number) =>
    api.get<ChannelAnalysisItem[]>("/dashboard/channel-analysis", { params: { year, month } }),
  // Feature 7: Comparison report
  comparison: (year: number, month: number) =>
    api.get<ComparisonReport>("/dashboard/comparison", { params: { year, month } }),
};

// ─── Reconciliation（对账：我们系统 vs bypms 原始）────────────────────────────
export const reconciliationApi = {
  get: (month: string) =>
    api.get("/reconciliation", { params: { month } }).then((r) => r.data),
  createMissing: (platform_order_id: string, room_id?: string | null) =>
    api.post("/reconciliation/create-missing", { platform_order_id, room_id }).then((r) => r.data),
  ignore: (platform_order_ids: string[]) =>
    api.post("/reconciliation/ignore", { platform_order_ids }).then((r) => r.data),
  link: (platform_order_id: string, order_id: string) =>
    api.post("/reconciliation/link", { platform_order_id, order_id }).then((r) => r.data),
  voidDuplicate: (order_id: string) =>
    api.post("/reconciliation/void-duplicate", { order_id }).then((r) => r.data),
  autoFix: (month: string, dry_run: boolean) =>
    api.post("/reconciliation/auto-fix", { month, dry_run }).then((r) => r.data),
};

// ─── Guests ───────────────────────────────────────────────────────────────────
export const guestsApi = {
  list: (params?: { keyword?: string; tag?: string; page?: number; page_size?: number }) =>
    api.get<GuestOut[]>("/guests", { params }),
  get: (id: string) => api.get<GuestOut>(`/guests/${id}`),
  lookup: (phone: string) => api.get<{ found: boolean; guest?: GuestOut }>("/guests/lookup", { params: { phone } }),
  update: (id: string, data: GuestUpdate) => api.patch<GuestOut>(`/guests/${id}`, data),
  orders: (id: string) => api.get<GuestOrderHistory[]>(`/guests/${id}/orders`),
  summary: () => api.get<GuestSummary>("/guests/stats/summary"),
};

// ─── Room Blocks ──────────────────────────────────────────────────────────────
export const roomBlocksApi = {
  list: (params?: { room_id?: string; year?: number; month?: number }) =>
    api.get<RoomBlockOut[]>("/room-blocks", { params }),
  create: (data: RoomBlockCreate) => api.post<RoomBlockOut>("/room-blocks", data),
  delete: (id: string) => api.delete(`/room-blocks/${id}`),
};

// ─── Settlements ──────────────────────────────────────────────────────────────
export const settlementsApi = {
  list: (params?: { owner_id?: string; billing_month?: string }) =>
    api.get<OwnerSettlementOut[]>("/settlements", { params }),
  detail: (id: string) => api.get<SettlementDetail>(`/settlements/${id}`),
  generate: (year: number, month: number, overwrite = false) =>
    api.post<{ generated: number; regenerated: number; skipped_locked: number; billing_month: string }>(
      "/settlements/generate", null, { params: { year, month, overwrite } }),
  confirm: (id: string) => api.post(`/settlements/${id}/confirm`),
  dispute: (id: string, notes: string) => api.post(`/settlements/${id}/dispute`, { notes }),
};

// ─── Notifications (Features 3 & 10) ─────────────────────────────────────────
export const notificationsApi = {
  list: (params?: { is_read?: boolean; page?: number; page_size?: number }) =>
    api.get<NotificationOut[]>("/notifications", { params }),
  markAsRead: (id: string) => api.post(`/notifications/${id}/read`),
  markAllAsRead: () => api.post("/notifications/read-all"),
  templates: () => api.get<NotificationTemplateOut[]>("/notifications/templates"),
  sendCheckinGuide: (data: CheckinGuideRequest) =>
    api.post<CheckinGuideResponse>("/notifications/send-checkin-guide", data),
  checkinCard: (orderId: string) => `/api/v1/notifications/checkin-card/${orderId}`,
};

// ─── Audit ─────────────────────────────────────────────────────────────────────
export const auditApi = {
  list: (params?: { page?: number; page_size?: number; resource_type?: string; resource_id?: string }) =>
    api.get("/audit-logs", { params }),
};

// ─── Export (Feature A) ──────────────────────────────────────────────────────
export const exportApi = {
  orders: (params?: { status?: string; check_in_from?: string; check_in_to?: string }) =>
    api.get("/export/orders", { params, responseType: "blob" }),
  finance: (params: { year?: number; month?: number; start_date?: string; end_date?: string }) =>
    api.get("/export/finance", { params, responseType: "blob" }),
  settlements: (billing_month?: string) =>
    api.get("/export/settlements", { params: { billing_month }, responseType: "blob" }),
  settlementStatement: (settlement_id: string) =>
    api.get(`/export/settlements/${settlement_id}/statement`, { responseType: "blob" }),
};

// ─── Search (Feature C) ─────────────────────────────────────────────────────
export const searchApi = {
  search: (q: string) =>
    api.get<SearchResult>("/search", { params: { q } }),
};

// ─── Batch (Feature E) ──────────────────────────────────────────────────────
export const batchApi = {
  transition: (order_ids: string[], target_status: string) =>
    api.post<{ succeeded: string[]; failed: Array<{ order_id: string; reason: string }> }>(
      "/orders/batch/transition",
      { order_ids, target_status }
    ),
};

// ─── 业主托管留资（tuoguan 落地页，公开接口） ────────────────────────────
export const hostingLeadsApi = {
  create: (payload: HostingLeadCreate) =>
    api
      .post<HostingLeadOut>("/hosting-leads", payload)
      .then((r) => r.data),
};

// ─── 账单对账（billing-recon，admin-only）────────────────────────────────
export interface ReconBatchDetail {
  batch: ReconBatchOut;
  diffs: ReconDiffOut[];
}

export const billingReconApi = {
  upload: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    // AI 列映射 + 对账可能要几十秒，放宽超时（不复用默认 25s，避免正常长任务被误判超时）。
    return api.post<ReconBatchDetail>("/billing-recon/upload", fd, {
      timeout: BILLING_RECON_UPLOAD_TIMEOUT_MS,
    });
  },
  batches: () => api.get<ReconBatchOut[]>("/billing-recon/batches"),
  batchDetail: (batchId: string) => api.get<ReconBatchDetail>(`/billing-recon/batches/${batchId}`),
  review: (batchId: string) =>
    api.post<{ reviewed_at: string; reviewed_by: string }>(`/billing-recon/batches/${batchId}/review`),
  archive: (batchId: string) =>
    api.post<{ archived_at: string }>(`/billing-recon/batches/${batchId}/archive`),
  // 端点是 /action（非 /act）——见 backend/app/api/v1/billing_recon.py，以代码为准。
  diffAction: (diffId: string, action: string) =>
    api.post<DiffActionResult>(`/billing-recon/diffs/${diffId}/action`, { action }),
  claim: (diffId: string, orderId: string) =>
    api.post<import("./billing-recon").ClaimResult>(
      `/billing-recon/diffs/${diffId}/claim`, { order_id: orderId }),
};

// ─── 经营助手（admin-only）──────────────────────────────────────────────
export interface AssistantTimelineRow {
  time: string;
  actor_label: string;
  actor_kind: string;
  is_human: boolean;
  verb: string;
  action: string;
  changes: string[];
  partial_snapshot: boolean;
  hedge: string;
  note: string | null;
}

export interface AssistantAnswer {
  kind: "forensics" | "metrics" | "reject" | "clarify";
  summary: string;
  timeline_text: string | null;
  timeline_rows: AssistantTimelineRow[] | null;
  data: Record<string, unknown> | null;
  orders: Array<Record<string, unknown>> | null;
  candidates: Array<Record<string, unknown>> | null;
  disclaimers: string[];
  narration_degraded: boolean;
  clarify_question: string | null;
  reject_reason: string | null;
}

export interface AssistantConversationMessage {
  role: "user" | "assistant";
  content: string;
}

export const assistantApi = {
  ask: (question: string, history: AssistantConversationMessage[] = []) =>
    api.post<AssistantAnswer>("/assistant/ask", { question, history }, { timeout: 60000 }),
};
