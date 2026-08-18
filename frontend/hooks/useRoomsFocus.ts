import { useEffect } from "react";
import { useFocusStore, resolveInitialFocus, readStoredFocus } from "@/lib/focus-store";
import { isMobileDevice } from "@/lib/responsive";

// 房态页全屏专注模式接线（从 rooms/page.tsx 抽出，便于测首帧竞态）。
// 专注模式是桌面专属。init effect 不能信 isMobile 参数：useIsMobile() 首帧
// 误报 false，闭包会带着假值抢先把 operator 默认专注写进全局 store（手机上
// 永不被重置）。effect 执行时 navigator 已可用，直接读真实 UA 掐死竞态。
export function useRoomsFocus(args: {
  isMobile: boolean;
  role: string | undefined;
  view: string;
  setView: (v: "gantt") => void;
}) {
  const { isMobile, role, view, setView } = args;
  const focusMode = useFocusStore((s) => s.focusMode);
  const initFocus = useFocusStore((s) => s.initFocus);
  const toggleFocus = useFocusStore((s) => s.toggleFocus);

  // 进入房态页时按「角色默认 + 手动记录」初始化（首帧普通模式，mount 后切换，规避 hydration 不一致）
  useEffect(() => {
    if (typeof navigator === "undefined" || isMobileDevice(navigator.userAgent)) return;
    initFocus(resolveInitialFocus(role, readStoredFocus()));
  }, [role, initFocus]);

  // 专注模式强制锁定甘特视图（退出全屏才能切单日/房卡）。
  // 手机永不锁（双保险）：老版本污染过的 store 或跨设备带来的 stored=1 都不该锁手机。
  useEffect(() => {
    if (!isMobile && focusMode && view !== "gantt") setView("gantt");
  }, [isMobile, focusMode, view, setView]);

  // 返回派生值 focusActive 而非裸 focusMode：裸值在手机上可能为 true（如 stored=1），
  // 消费方一律走 !isMobile 口径的 focusActive，别把裸值加回返回值。
  const focusActive = !isMobile && focusMode && view === "gantt";
  return { toggleFocus, focusActive };
}
