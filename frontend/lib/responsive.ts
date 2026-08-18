import { useEffect, useState, useSyncExternalStore } from "react";
import { MOBILE_DEVICE_CLASS, isMobileDevice } from "./mobile-device";

// 统一断点定义，与 Tailwind 保持一致
export const breakpoints = {
  sm: 640,
  md: 768,
  lg: 1024,
  xl: 1280,
} as const;

// useMediaQuery hook
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const media = window.matchMedia(query);
    setMatches(media.matches);
    const listener = () => setMatches(media.matches);
    media.addEventListener("change", listener);
    return () => media.removeEventListener("change", listener);
  }, [query]);

  return matches;
}

export const isMobileQuery = "(max-width: 1023px)";
export const isTabletQuery = "(min-width: 768px) and (max-width: 1023px)";
export const isDesktopQuery = "(min-width: 1024px)";

// UA 判定与设备类名的真相源在 lib/mobile-device.ts（零 React 依赖，layout.tsx
// 的内联脚本也从那里插值生成）。这里 re-export 维持既有 import 路径不变。
export { MOBILE_DEVICE_CLASS, MOBILE_UA_REGEX, isMobileDevice } from "./mobile-device";

// 客户端真相源：app/layout.tsx 挂载前内联脚本按 UA 给 <html> 打的 MOBILE_DEVICE_CLASS 类。
// 读类而不是直接读 navigator.userAgent，保证 JS 分支与结构性 CSS（隐藏侧栏/底部导航等
// 同挂这个类）永远同一判定，不会出现「CSS 认手机、JS 认桌面」的劈叉。
// 设备类型在页面生命周期内不变，无需监听变化；订阅时机只做一次 UA 对账：
// 内联脚本是打类的唯一执行者，若它没执行（未来加了 CSP、根布局被换），手机会整体
// 静默降级成桌面版——对账发现发散只告警不自愈，自愈（回退读 UA）会造成
// 「CSS 认桌面、JS 认手机」的劈叉，比整体降级更糟。
let deviceClassAudited = false;
const subscribeDeviceType = () => {
  if (
    !deviceClassAudited &&
    process.env.NODE_ENV !== "test" &&
    typeof document !== "undefined" &&
    typeof navigator !== "undefined"
  ) {
    deviceClassAudited = true;
    if (isMobileDevice(navigator.userAgent) !== mobileSnapshot()) {
      console.warn(
        `[responsive] ${MOBILE_DEVICE_CLASS} 类与 UA 判定不一致——app/layout.tsx 内联脚本可能没执行（CSP 拦截或根布局变更）`,
      );
    }
  }
  return () => {};
};
const mobileSnapshot = () =>
  typeof document !== "undefined" && document.documentElement.classList.contains(MOBILE_DEVICE_CLASS);
const desktopSnapshot = () => !mobileSnapshot();
const serverSnapshotFalse = () => false;

// 移动端布局判断 hook —— 按设备类型，跨窗口缩放稳定不变。
// 取代 useMediaQuery(isMobileQuery)：后者随窗口宽度翻转，桌面拉窄会错切手机版。
//
// 首帧语义（用 useSyncExternalStore 而非 useState+useEffect 校正，两点都是刻意的）：
// - CSR 挂载（auth 门闩后的后台页面、路由跳转、弹窗）：首帧即真值。一次性初始化
//   effect 的 isMobile 闭包不再吃到假 false（专注模式锁视图 bug 的根因）。
// - SSR / hydration（/login、/owner 这类直渲染页）：hydration 帧用 server 快照 false
//   与服务端 HTML 对齐（不报 mismatch），commit 时发现真快照不同会在 paint 前同步重渲。
//   若改成 useState 初始值直接读类，手机上 hydration 首帧树就和服务端不一致，必报错。
export function useIsMobile(): boolean {
  return useSyncExternalStore(subscribeDeviceType, mobileSnapshot, serverSnapshotFalse);
}

// 桌面布局判断 hook —— 与 useIsMobile() 互补，但 SSR / hydration 帧返回 false（= 按移动端渲染）。
// 用途：业主端等「移动优先、桌面增强」的页面。手机是主力设备，SSR HTML 按移动版出，
// 只有真桌面设备在 hydration 后升级为 true，reflow 一次；CSR 挂载则首帧即真值。
// 与 useIsMobile() 的回退值语义相反是刻意的：那个假设桌面，这个假设移动。
export function useIsDesktop(): boolean {
  return useSyncExternalStore(subscribeDeviceType, desktopSnapshot, serverSnapshotFalse);
}

