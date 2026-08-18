import axios, { AxiosError } from "axios";

export const OWNER_TOKEN_KEY = "owner_access_token";

export const ownerApi = axios.create({
  baseURL: process.env.NEXT_PUBLIC_API_URL || "/api/v1",
  // 与主 api 对齐：避免后端慢查询导致浏览器一直转圈打不开页面。
  // Neon 冷启动最坏 ~18s 仍能完成；超过 25s 几乎一定是真出问题。
  timeout: 25000,
});

ownerApi.interceptors.request.use((config) => {
  if (typeof window !== "undefined") {
    const token = localStorage.getItem(OWNER_TOKEN_KEY);
    if (token) config.headers["Authorization"] = `Bearer ${token}`;
  }
  return config;
});

ownerApi.interceptors.response.use(
  (res) => res,
  async (error: AxiosError) => {
    if (error.response?.status === 401 && typeof window !== "undefined") {
      localStorage.removeItem(OWNER_TOKEN_KEY);
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      if (!window.location.pathname.startsWith("/owner/login")) {
        window.location.href = `/owner/login?next=${next}`;
      }
    }
    return Promise.reject(error);
  }
);

// ─── Types ────────────────────────────────────────────────────────────────────

export interface OwnerSubBrief {
  owner_id: string;
  name: string;
  room_count: number;
}

export interface OwnerProfile {
  owner_id: string;
  name: string;
  username: string | null;
  phone: string | null;
  room_count: number;
  is_master?: boolean;
  sub_owners?: OwnerSubBrief[];
}

export interface OwnerToken {
  access_token: string;
  token_type: string;
  expires_in: number;
  owner: OwnerProfile;
}

export interface RoomMonthlyStat {
  room_id: string;
  room_name: string;
  room_type: string | null;
  owner_id?: string;
  owner_name?: string;
  owner_share_ratio: string;
  order_count: number;
  occupancy_nights: number;
  revenue: string;
  net_revenue: string;
  owner_expenses: string;
  owner_net: string;
}

export interface MonthlySummary {
  year: number;
  month: number;
  billing_month: string;
  rooms_total: number;
  order_count: number;
  revenue: string;
  net_revenue: string;
  owner_expenses: string;
  owner_net: string;
  // 「结算到昨天」截止日（YYYY-MM-DD）：仅查看当前月时有值，提示业主这是
  // 阶段性数字、本月未退房订单退房后陆续计入；查看历史月为 null。
  as_of_date?: string | null;
  rooms: RoomMonthlyStat[];
}

export interface OrderLite {
  order_id: string;
  channel: string;
  guest_name: string;
  check_in_date: string;
  check_out_date: string;
  nights: number;
  actual_price: string | null;
  commission_rate: string;
  order_status: string;
}

export interface RoomDetail {
  room_id: string;
  room_name: string;
  year: number;
  month: number;
  stat: RoomMonthlyStat;
  orders: OrderLite[];
}

export interface SettlementBrief {
  settlement_id: string;
  owner_id?: string;
  owner_name?: string;
  billing_month: string;
  actual_owner_amount: string;
  // 账面「到厘」展示值（打款仍用 actual_owner_amount 到分值）；旧结算可能缺省
  actual_owner_amount_precise?: string | null;
  status: string;
  created_at: string;
}

export interface SettlementItemDetail {
  item_id: string;
  // 业主级支出行（整层/某业主电费）room_id 为空，用 label 展示。
  room_id: string | null;
  room_name: string | null;
  label?: string | null;
  order_count: number;
  revenue: string;
  commission: string;
  net_revenue: string;
  owner_expenses: string;
  share_ratio_snapshot: string;
  owner_net_amount: string;
  // 该房「业主应得·到厘」展示值（打款仍用 owner_net_amount 到分值）；旧结算可能缺省
  owner_net_amount_precise?: string | null;
  // v2#4: 每笔费用按 RoomCostShareRule 加权后的明细（业主端 v2#5 展示）
  cost_share_breakdown?: Array<{
    expense_id: string;
    category: string;
    booking_type: string;
    amount: string;
    share_percent: string;
    owner_amount: string;
    rule_id?: string | null;
  }>;
}

// 当前服务费费率（后端 /owner/me/settlements/{id} 下发）。
// 业主端据此把金额还原成「¥65 × 85次」；对不上时降级成「N笔」。
export interface ServiceFeeRates {
  checkout_cleaning_fee: string;
  instay_cleaning_fee: string;
  laundry_fee_per_room: string;
  consumable_fee_per_room_night: string;
}

export interface SettlementDetail {
  settlement_id: string;
  billing_month: string;
  total_net_revenue: string;
  owner_amount: string;
  deducted_expenses: string;
  actual_owner_amount: string;
  // 账面「到厘」展示值（打款仍用上面到分的 owner_amount/actual_owner_amount）；旧结算可能缺省
  owner_amount_precise?: string | null;
  actual_owner_amount_precise?: string | null;
  status: string;
  notes: string | null;
  service_fee_rates?: Partial<ServiceFeeRates>;
  items: SettlementItemDetail[];
}

// 业主自助房态：故意不暴露 guest_name/guest_phone/订单金额/收款状态等 PII。
// 仅展示占用状态 + 渠道，业主可"判断房间是否真有人住"，不触碰客人个人信息。
export interface OwnerCalendarDay {
  // OrderStatus 取值或 "block:{maintenance|owner_use|reserved|other}"
  status: string;
  channel: string | null;
  is_check_in_day: boolean;
  is_check_out_day: boolean;
}

export interface OwnerCalendarRoom {
  room_id: string;
  room_name: string;
  room_type: string | null;
  floor: number | null;
  room_status: string;
  days: Record<string, OwnerCalendarDay>;
  // 总账号（is_master）分层展示用：非 master 场景后端也可能不下发,前端一律按可选处理。
  owner_id?: string;
  owner_name?: string;
}

// ─── APIs ─────────────────────────────────────────────────────────────────────

export const ownerAuthApi = {
  sendOtp: (phone: string) =>
    ownerApi.post<{ sent: boolean }>("/owner/auth/send-otp", { phone }),
  verify: (phone: string, code: string) =>
    ownerApi.post<OwnerToken>("/owner/auth/verify", { phone, code }),
  loginPassword: (username: string, password: string) =>
    ownerApi.post<OwnerToken>("/owner/auth/login-password", { username, password }),
  me: () => ownerApi.get<OwnerProfile>("/owner/me"),
  changePassword: (current_password: string, new_password: string) =>
    ownerApi.post("/owner/me/password", { current_password, new_password }),
};

export interface RoomImage {
  image_id: string;
  room_id: string;
  url: string;
  sort_order: number;
  is_cover: boolean;
  content_type: string | null;
  created_at: string;
}

export const ownerDataApi = {
  summary: (year?: number, month?: number) =>
    ownerApi.get<MonthlySummary>("/owner/me/summary", { params: { year, month } }),
  rooms: (year?: number, month?: number) =>
    ownerApi.get<RoomMonthlyStat[]>("/owner/me/rooms", { params: { year, month } }),
  roomDetail: (roomId: string, year?: number, month?: number) =>
    ownerApi.get<RoomDetail>(`/owner/me/rooms/${roomId}`, { params: { year, month } }),
  settlements: () => ownerApi.get<SettlementBrief[]>("/owner/me/settlements"),
  settlementDetail: (id: string) =>
    ownerApi.get<SettlementDetail>(`/owner/me/settlements/${id}`),
  calendar: (year?: number, month?: number) =>
    ownerApi.get<OwnerCalendarRoom[]>("/owner/me/calendar", { params: { year, month } }),
  images: {
    list: (roomId: string) =>
      ownerApi.get<RoomImage[]>(`/owner/me/rooms/${roomId}/images`),
    upload: (roomId: string, file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      return ownerApi.post<RoomImage>(`/owner/me/rooms/${roomId}/images`, fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
    },
    patch: (roomId: string, imageId: string, body: { is_cover?: boolean; sort_order?: number }) =>
      ownerApi.patch<RoomImage>(`/owner/me/rooms/${roomId}/images/${imageId}`, body),
    delete: (roomId: string, imageId: string) =>
      ownerApi.delete(`/owner/me/rooms/${roomId}/images/${imageId}`),
    reorder: (roomId: string, orderedIds: string[]) =>
      ownerApi.post<RoomImage[]>(`/owner/me/rooms/${roomId}/images/reorder`, { ordered_ids: orderedIds }),
  },
};
