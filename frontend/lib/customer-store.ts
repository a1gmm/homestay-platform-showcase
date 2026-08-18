import { create } from "zustand";
import { persist } from "zustand/middleware";
import { CUSTOMER_TOKEN_KEY, type CustomerOut } from "./customer-api";

interface CustomerState {
  customer: CustomerOut | null;
  access_token: string | null;
  setAuth: (customer: CustomerOut, token: string) => void;
  clearAuth: () => void;
  setCustomer: (customer: CustomerOut) => void;
  isLoggedIn: () => boolean;
}

export const useCustomerStore = create<CustomerState>()(
  persist(
    (set, get) => ({
      customer: null,
      access_token: null,
      setAuth: (customer, access_token) => {
        if (typeof window !== "undefined") {
          localStorage.setItem(CUSTOMER_TOKEN_KEY, access_token);
        }
        set({ customer, access_token });
      },
      setCustomer: (customer) => set({ customer }),
      clearAuth: () => {
        if (typeof window !== "undefined") {
          localStorage.removeItem(CUSTOMER_TOKEN_KEY);
        }
        set({ customer: null, access_token: null });
      },
      isLoggedIn: () => !!get().customer && !!get().access_token,
    }),
    {
      name: "customer-auth",
      partialize: (s) => ({ customer: s.customer, access_token: s.access_token }),
    }
  )
);
