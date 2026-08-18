import { create } from "zustand";

const STORAGE_KEY = "focus_mode";

/** 读手动选择偏好；无记录返回 null（SSR 下也返回 null）。 */
export function readStoredFocus(): boolean | null {
  if (typeof window === "undefined") return null;
  const v = window.localStorage.getItem(STORAGE_KEY);
  if (v === "1") return true;
  if (v === "0") return false;
  return null;
}

/** 写手动选择到 localStorage。 */
export function writeStoredFocus(on: boolean): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, on ? "1" : "0");
}

/**
 * 进入房态页时专注模式初值。
 * 手动记录优先于角色默认：stored 非 null 时以 stored 为准；
 * 无记录时仅 operator 自动进入。
 */
export function resolveInitialFocus(
  role: string | undefined,
  stored: boolean | null,
): boolean {
  if (stored !== null) return stored;
  return role === "operator";
}

/** 是否应隐藏系统 chrome（侧栏/顶栏）。layout 用它；page 另判 view。 */
export function shouldHideChrome(args: {
  isMobile: boolean;
  pathname: string;
  focusMode: boolean;
}): boolean {
  return !args.isMobile && args.pathname === "/rooms" && args.focusMode;
}

interface FocusState {
  focusMode: boolean;
  /** 手动设置：落 localStorage（手动选择会覆盖角色默认）。 */
  setFocusMode: (on: boolean) => void;
  toggleFocus: () => void;
  /** 初始化：只改内存状态，不写 localStorage。 */
  initFocus: (on: boolean) => void;
}

export const useFocusStore = create<FocusState>((set, get) => ({
  focusMode: false,
  setFocusMode: (on) => {
    writeStoredFocus(on);
    set({ focusMode: on });
  },
  toggleFocus: () => get().setFocusMode(!get().focusMode),
  initFocus: (on) => set({ focusMode: on }),
}));
