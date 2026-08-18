import { create } from "zustand";
import { persist } from "zustand/middleware";
import { OWNER_TOKEN_KEY, type OwnerProfile } from "./owner-api";

interface OwnerAuthState {
  owner: OwnerProfile | null;
  access_token: string | null;
  setAuth: (owner: OwnerProfile, token: string) => void;
  clearAuth: () => void;
  isLoggedIn: () => boolean;
}

export const useOwnerStore = create<OwnerAuthState>()(
  persist(
    (set, get) => ({
      owner: null,
      access_token: null,
      setAuth: (owner, access_token) => {
        if (typeof window !== "undefined") {
          localStorage.setItem(OWNER_TOKEN_KEY, access_token);
        }
        set({ owner, access_token });
      },
      clearAuth: () => {
        if (typeof window !== "undefined") {
          localStorage.removeItem(OWNER_TOKEN_KEY);
        }
        set({ owner: null, access_token: null });
      },
      isLoggedIn: () => !!get().owner && !!get().access_token,
    }),
    {
      name: "owner-auth",
      partialize: (s) => ({ owner: s.owner, access_token: s.access_token }),
    }
  )
);
