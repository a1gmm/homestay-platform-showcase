import { create } from "zustand";
import { mergeSeen, type SeenItem } from "@/lib/new-order-alert";

const MUTED_KEY = "new_order_muted";

/**
 * localStorage 读写包一层。
 * 从 globalThis 取而非 window：浏览器里两者等价，SSR 时 globalThis.localStorage
 * 本就 undefined（保护同样成立），且这样才可在 node 测试环境里被 stub 到。
 * 全程 try/catch：无痕模式会抛，存储异常不该拖垮提示功能。
 */
function storage(): Storage | undefined {
  return (globalThis as unknown as { localStorage?: Storage }).localStorage;
}
function readMuted(): boolean {
  try {
    return storage()?.getItem(MUTED_KEY) === "1";
  } catch {
    return false;
  }
}
function writeMuted(muted: boolean): void {
  try {
    storage()?.setItem(MUTED_KEY, muted ? "1" : "0");
  } catch {
    // 写不了盘：内存态仍生效，只是刷新后不记得，可接受
  }
}
function clearMuted(): void {
  try {
    storage()?.removeItem(MUTED_KEY);
  } catch {
    // 同上
  }
}

interface NewOrderState {
  /** 见过的订单 id → created_at。放 store 而非组件 ref：建单入口要跨组件 markSeen。 */
  seen: Map<string, string>;
  /** 基线是否已取。放 store：否则 watcher 重挂载会重取基线，冲掉已标记的自录单。 */
  baselineTaken: boolean;
  /**
   * 落 localStorage 持久化（2026-07-17 起）。
   * 原先只管当次会话，因为「一键就能静音而全系统没有取消入口」——持久化等于误点
   * 一次功能永久归零且无人知晓。顶栏喇叭图标上线后那个理由不成立了：一眼能看出
   * 当前是否静音、随时点回来。**⚠️ 若哪天那个图标被删，必须把持久化一并撤回。**
   * 换班仍要清盘，见 reset()。
   */
  muted: boolean;
  /** 标记为「已知」——首次基线 + 前台自己录的单都走它。 */
  markSeen: (items: SeenItem[]) => void;
  /** 首次响应：灌基线并置位，不弹窗。 */
  takeBaseline: (items: SeenItem[]) => void;
  setMuted: (muted: boolean) => void;
  /** 顶栏喇叭图标用。 */
  toggleMuted: () => void;
  /** 挂载时从 localStorage 恢复偏好（SSR 期读不到，故不能放 store 初值）。 */
  hydrateMuted: () => void;
  /**
   * 登出时必须调（见 (dashboard)/layout.tsx 的 handleLogout）。
   * 生产域名 admin.* 上「登出→登录」全程是 router.push 软跳转，不刷页面，
   * 这个模块级 store 会原封不动带到下一个人手里。前台换班就是这个场景：
   *  - A 关过提示音 → B 静音上岗（顶栏图标能看出来，但不该让她自己去发现）
   *  - baselineTaken 不重置 → B 拿 A 的基线判新
   */
  reset: () => void;
}

export const useNewOrderStore = create<NewOrderState>((set, get) => ({
  seen: new Map(),
  baselineTaken: false,
  muted: false,
  markSeen: (items) => set({ seen: mergeSeen(get().seen, items) }),
  takeBaseline: (items) => set({ seen: mergeSeen(get().seen, items), baselineTaken: true }),
  setMuted: (muted) => {
    writeMuted(muted);
    set({ muted });
  },
  toggleMuted: () => {
    const next = !get().muted;
    writeMuted(next);
    set({ muted: next });
  },
  hydrateMuted: () => set({ muted: readMuted() }),
  reset: () => {
    // 必须连 localStorage 一起清：登出→登录不刷页面（内存态本就被下面这行清了），
    // 但落盘的偏好会活过刷新——不清的话下一班刷新一下又静音了。
    clearMuted();
    set({ seen: new Map(), baselineTaken: false, muted: false });
  },
}));
