import { OWNER_TOKEN_KEY } from "./owner-api";

/**
 * 读取本地已保存的业主登录 token。
 * 与 ownerApi 请求拦截器同源(localStorage[OWNER_TOKEN_KEY]),保证「页面认为已登录」
 * 与「请求实际带的 token」永远一致。SSR / 无 window 时返回 null。
 */
export function getStoredOwnerToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(OWNER_TOKEN_KEY);
}

/**
 * 业主打开登录页时的「已登录免登录」入口决策。
 *
 * - 本地有 token → 返回目标路径,登录页直接 replace 过去,业主 30 天有效期内不再手输账号密码。
 * - 没有 token → 返回 null,正常展示登录表单。
 *
 * 注:token 可能已过期/被吊销。跳转后目标页首个接口返回 401 时,owner-api 拦截器会清掉
 * token 并送回 /owner/login,此时本函数返回 null → 展示表单,不会死循环。
 *
 * next 做了 open-redirect 防护:只允许站内绝对路径(单个 "/" 开头),其余一律兜底到 /owner。
 */
export function resolveOwnerLoginEntry(token: string | null, next: string): string | null {
  if (!token || !token.trim()) return null;
  const isInternalPath = !!next && next.startsWith("/") && !next.startsWith("//");
  return isInternalPath ? next : "/owner";
}
