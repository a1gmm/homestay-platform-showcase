import { create } from "zustand";
import { persist } from "zustand/middleware";
import { STAFF_TOKEN_KEY } from "./staff-api";

export interface StaffUser {
  user_id: string;
  display_name: string;
  role: string; // admin | operator | finance | cleaner | keeper | owner
}

interface StaffState {
  user: StaffUser | null;
  access_token: string | null;
  setAuth: (user: StaffUser, token: string) => void;
  clearAuth: () => void;
  isLoggedIn: () => boolean;
}

export const useStaffStore = create<StaffState>()(
  persist(
    (set, get) => ({
      user: null,
      access_token: null,
      setAuth: (user, access_token) => {
        if (typeof window !== "undefined") {
          localStorage.setItem(STAFF_TOKEN_KEY, access_token);
        }
        set({ user, access_token });
      },
      clearAuth: () => {
        if (typeof window !== "undefined") {
          localStorage.removeItem(STAFF_TOKEN_KEY);
        }
        set({ user: null, access_token: null });
      },
      isLoggedIn: () => !!get().user && !!get().access_token,
    }),
    {
      name: "staff-auth",
      partialize: (s) => ({ user: s.user, access_token: s.access_token }),
    }
  )
);
