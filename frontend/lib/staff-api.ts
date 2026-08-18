import axios, { AxiosError } from "axios";

export const STAFF_TOKEN_KEY = "staff_access_token";

export const staffApi = axios.create({
  baseURL: process.env.NEXT_PUBLIC_API_URL || "/api/v1",
});

staffApi.interceptors.request.use((config) => {
  if (typeof window !== "undefined") {
    const token = localStorage.getItem(STAFF_TOKEN_KEY);
    if (token) config.headers["Authorization"] = `Bearer ${token}`;
  }
  return config;
});

staffApi.interceptors.response.use(
  (res) => res,
  async (error: AxiosError) => {
    if (error.response?.status === 401 && typeof window !== "undefined") {
      localStorage.removeItem(STAFF_TOKEN_KEY);
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      if (!window.location.pathname.startsWith("/staff/login")) {
        window.location.href = `/staff/login?next=${next}`;
      }
    }
    return Promise.reject(error);
  }
);

// ─── Types ────────────────────────────────────────────────────────────────────

export interface StaffAuthResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  user_id: string;
  role: string;
  display_name: string;
}

export interface StaffTask {
  task_id: string;
  task_type: string;
  title: string;
  description: string | null;
  order_id: string | null;
  room_id: string | null;
  assignee_id: string | null;
  status: string;
  priority: string;
  deadline: string | null;
  completed_at: string | null;
  notes: string | null;
  submitted_at: string | null;
  review_status: string | null;
  rejection_reason: string | null;
  created_at: string;
}

export interface StaffOrderItem {
  order_id: string;
  channel: string;
  guest_name: string;
  guest_phone: string;
  room_id: string | null;
  check_in_date: string;
  check_out_date: string;
  nights: number;
  actual_price: string | null;
  deposit?: string | number | null;
  deposit_status?: string;
  deposit_returned?: string | number | null;
  order_status: string;
  payment_status: string;
  cleaning_status?: string;
  created_at: string;
}

export interface CalendarDay {
  [date: string]: {
    order_id: string | null;
    guest_name: string;
    status: string;
  };
}

export interface CalendarRoom {
  room_id: string;
  room_name: string;
  room_status: string;
  days: Record<string, { order_id: string | null; guest_name: string; status: string }>;
}

// ─── APIs ─────────────────────────────────────────────────────────────────────

export const staffAuthApi = {
  sendOtp: (phone: string) =>
    staffApi.post<{ sent: boolean }>("/staff/auth/send-otp", { phone }),
  verify: (phone: string, code: string) =>
    staffApi.post<StaffAuthResponse>("/staff/auth/verify", { phone, code }),
};

export interface CleanerBrief {
  user_id: string;
  display_name: string;
  phone: string | null;
}

export interface HandleResult {
  order_id: string;
  order_status: string;
  created_task_id?: string | null;
}

export const staffDataApi = {
  tasks: (params?: { status?: string; overdue_only?: boolean }) =>
    staffApi.get<StaffTask[]>("/tasks", { params }),
  submitTask: (taskId: string, notes?: string) =>
    staffApi.post<StaffTask>(`/tasks/${taskId}/submit`, { notes }),
  updateTaskStatus: (taskId: string, status: string) =>
    staffApi.patch<StaffTask>(`/tasks/${taskId}`, { status }),
  reviewTask: (taskId: string, approved: boolean, rejection_reason?: string) =>
    staffApi.post<StaffTask>(`/tasks/${taskId}/review`, { approved, rejection_reason }),

  ordersList: (params: {
    check_in_from?: string;
    check_in_to?: string;
    check_out_from?: string;
    check_out_to?: string;
    status?: string;
    page?: number;
    page_size?: number;
  }) => staffApi.get<{ items: StaffOrderItem[]; total: number }>("/orders", { params }),

  roomsCalendar: (year: number, month: number) =>
    staffApi.get<CalendarRoom[]>("/rooms/availability/calendar", {
      params: { year, month },
    }),

  updateRoomStatus: (roomId: string, room_status: string) =>
    staffApi.patch(`/rooms/${roomId}`, { room_status }),

  // 管家操作
  listCleaners: () => staffApi.get<CleanerBrief[]>("/staff/cleaners"),
  // 2026-06-05：办理入住可同时收押金（collect_deposit 默认 true）。
  handleCheckin: (orderId: string, notes?: string, collect_deposit = true) =>
    staffApi.post<HandleResult>(`/staff/orders/${orderId}/handle-checkin`, { notes, collect_deposit }),
  // 2026-06-05：办理退房可结算押金（deposit_refund_amount 不传 = 全退；少退须填原因）。
  handleCheckout: (
    orderId: string,
    cleaner_id?: string, // 选填（批3 item1「暂不派单」）：留空 = 建未分配清扫任务进待派池
    notes?: string,
    deposit?: { deposit_refund_amount?: number; deposit_withhold_reason?: string },
  ) =>
    staffApi.post<HandleResult>(`/staff/orders/${orderId}/handle-checkout`, {
      cleaner_id: cleaner_id || null,
      notes,
      ...deposit,
    }),
};
