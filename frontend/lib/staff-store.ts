import { create } from "zustand";
import { persist } from "zustand/middleware";
import { STAFF_TOKEN_KEY } from "./staff-api";
import { clearAnnouncementSnooze } from "./release-announcement-snooze";

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
        clearAnnouncementSnooze(user.user_id);
        if (typeof window !== "undefined") {
          localStorage.setItem(STAFF_TOKEN_KEY, access_token);
        }
        set({ user, access_token });
      },
      clearAuth: () => {
        const userId = get().user?.user_id;
        if (userId) clearAnnouncementSnooze(userId);
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
