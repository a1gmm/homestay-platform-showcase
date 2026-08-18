import { create } from "zustand";
import { persist } from "zustand/middleware";

/** Lightweight user info stored in auth state (from login/token response) */
export interface AuthUser {
  user_id: string;
  role: string;
  display_name: string;
}

interface AuthState {
  user: AuthUser | null;
  access_token: string | null;
  setAuth: (user: AuthUser, token: string, refresh: string) => void;
  clearAuth: () => void;
  isAdmin: () => boolean;
  isOperator: () => boolean;
  isFinance: () => boolean;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      user: null,
      access_token: null,
      setAuth: (user, access_token, refresh_token) => {
        localStorage.setItem("access_token", access_token);
        localStorage.setItem("refresh_token", refresh_token);
        set({ user, access_token });
      },
      clearAuth: () => {
        localStorage.removeItem("access_token");
        localStorage.removeItem("refresh_token");
        localStorage.removeItem("last_activity_at");
        set({ user: null, access_token: null });
      },
      isAdmin: () => get().user?.role === "admin",
      isOperator: () => ["admin", "operator"].includes(get().user?.role ?? ""),
      isFinance: () => ["admin", "finance"].includes(get().user?.role ?? ""),
    }),
    { name: "auth-storage", partialize: (s) => ({ user: s.user }) }
  )
);
