import { handoffApi, type HandoffPayload } from "@/lib/api";

export const HANDOFF_RETRY_MESSAGE = "登录交接暂不可用，请稍后重试登录。";

/** Only a short-lived, one-time code may cross origins in the browser URL. */
export async function createHandoffUrl(origin: string, payload: HandoffPayload): Promise<string> {
  const { data } = await handoffApi.create(payload);
  if (typeof data.code !== "string" || !data.code.trim()) {
    throw new Error("Missing login handoff code");
  }
  const target = new URL("/auth/accept", origin);
  target.searchParams.set("code", data.code);
  return target.toString();
}

/** router.replace accepts executable/external URLs; only allow local paths here. */
export function safeHandoffNext(value: string | null | undefined, fallback: string): string {
  if (!value?.startsWith("/")) return fallback;
  try {
    const decoded = decodeURIComponent(value);
    if (decoded.startsWith("//") || /[\\\u0000-\u0020\u007f]/.test(decoded)) return fallback;
    const target = new URL(value, "https://handoff.invalid");
    if (target.origin !== "https://handoff.invalid") return fallback;
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return fallback;
  }
}
