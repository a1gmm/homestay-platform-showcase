import axios, { AxiosError } from "axios";

export const CUSTOMER_TOKEN_KEY = "customer_access_token";

export const customerApi = axios.create({
  baseURL: process.env.NEXT_PUBLIC_API_URL || "/api/v1",
});

customerApi.interceptors.request.use((config) => {
  if (typeof window !== "undefined") {
    const token = localStorage.getItem(CUSTOMER_TOKEN_KEY);
    if (token) config.headers["Authorization"] = `Bearer ${token}`;
  }
  return config;
});

customerApi.interceptors.response.use(
  (res) => res,
  async (error: AxiosError) => {
    if (error.response?.status === 401 && typeof window !== "undefined") {
      localStorage.removeItem(CUSTOMER_TOKEN_KEY);
      // Hard redirect so pages re-evaluate auth guard
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      if (!window.location.pathname.startsWith("/booking/login")) {
        window.location.href = `/booking/login?next=${next}`;
      }
    }
    return Promise.reject(error);
  }
);

// ─── Types ────────────────────────────────────────────────────────────────────

export interface RoomPublic {
  room_id: string;
  room_name: string;
  floor: number | null;
  base_price: string | null;
  province: string | null;
  city: string | null;
  district: string | null;
  community_name: string | null;
  min_stay_nights: number;
  images: string[];
  description: string | null;
  amenities: string[];
}

export interface CalendarDay {
  date: string; // YYYY-MM-DD
  price: string | null;
  available: boolean;
}

export interface CustomerOut {
  phone: string;
  name: string | null;
  id_number: string | null;
  created_at: string;
}

export interface CustomerToken {
  access_token: string;
  token_type: string;
  expires_in: number;
  customer: CustomerOut;
}

export interface BookingOrderOut {
  order_id: string;
  room_id: string;
  check_in_date: string;
  check_out_date: string;
  nights: number;
  guest_name: string;
  guest_phone: string;
  list_price: string | null;
  actual_price: string | null;
  order_status: string;
  payment_status: string;
  created_at: string;
}

export interface MyOrderItem {
  order_id: string;
  channel: string;
  guest_name: string;
  guest_phone: string;
  room_id: string | null;
  check_in_date: string;
  check_out_date: string;
  nights: number;
  actual_price: string | null;
  order_status: string;
  payment_status: string;
  created_at: string;
}

// ─── APIs ─────────────────────────────────────────────────────────────────────

export const bookingApi = {
  listRooms: (params?: { check_in?: string; check_out?: string }) =>
    customerApi.get<RoomPublic[]>("/booking/rooms", { params }),
  getRoom: (roomId: string) => customerApi.get<RoomPublic>(`/booking/rooms/${roomId}`),
  calendar: (roomId: string, start: string, end: string) =>
    customerApi.get<CalendarDay[]>(`/booking/rooms/${roomId}/calendar`, {
      params: { start, end },
    }),
  createOrder: (data: {
    room_id: string;
    check_in_date: string;
    check_out_date: string;
    guest_name: string;
    id_number?: string;
    notes?: string;
  }) => customerApi.post<BookingOrderOut>("/booking/orders", data),
};

export const customerAuthApi = {
  sendOtp: (phone: string) =>
    customerApi.post<{ sent: boolean }>("/customer/auth/send-otp", { phone }),
  verify: (phone: string, code: string, name?: string) =>
    customerApi.post<CustomerToken>("/customer/auth/verify", { phone, code, name }),
  me: () => customerApi.get<CustomerOut>("/customer/me"),
  updateMe: (data: { name?: string; id_number?: string }) =>
    customerApi.patch<CustomerOut>("/customer/me", data),
  myOrders: () => customerApi.get<MyOrderItem[]>("/customer/me/orders"),
};
