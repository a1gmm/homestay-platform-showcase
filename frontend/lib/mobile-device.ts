// 设备判定的唯一真相源——纯常量/纯函数，零 React 依赖。
// 单独成文件是刚性约束：app/layout.tsx（server component）要 import 这里的常量
// 来插值生成挂载前内联脚本，而 server component 不能 import 引了 React hooks 的
// 模块（lib/responsive.ts 里是 hook 层）。判据改这里，脚本、CSS 类、hook 全线同步。
// 用 "Mobi" token（MDN 推荐信号，iPhone/Android 手机 UA 均含）判断，平板/桌面无此 token。
export const MOBILE_UA_REGEX = /Android.*Mobile|iPhone|iPod|Windows Phone|Mobi/i;
export const MOBILE_DEVICE_CLASS = "is-mobile-device";

// 是否手机设备 —— 纯函数，只看 UA，不看窗口宽度。
// 客户反馈：桌面浏览器拉窄窗口就整体翻转成手机版（甘特图选中日期弹回今天）。
// 根因是移动/桌面模式由视口宽度（max-width:1023px）决定。改为按设备类型判断：
// 电脑无论窗口怎么缩放都是桌面版；只有真手机才是移动版。
export function isMobileDevice(userAgent: string): boolean {
  return MOBILE_UA_REGEX.test(userAgent);
}
