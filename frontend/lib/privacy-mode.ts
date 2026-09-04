export const PRIVACY_MODE_STORAGE_KEY = "admin_privacy_mode";

function getSessionStorage(): Storage | undefined {
  try {
    return (globalThis as typeof globalThis & { sessionStorage?: Storage }).sessionStorage;
  } catch {
    return undefined;
  }
}

export function readPrivacyMode(): boolean {
  try {
    return getSessionStorage()?.getItem(PRIVACY_MODE_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function writePrivacyMode(on: boolean): void {
  try {
    const storage = getSessionStorage();
    if (!storage) return;
    if (on) storage.setItem(PRIVACY_MODE_STORAGE_KEY, "1");
    else storage.removeItem(PRIVACY_MODE_STORAGE_KEY);
  } catch {
    // Storage restrictions must not break the dashboard; the reload will fall
    // back to normal mode because no durable per-tab flag could be written.
  }
}

export function clearPrivacyMode(): void {
  try {
    getSessionStorage()?.removeItem(PRIVACY_MODE_STORAGE_KEY);
  } catch {
    // Best-effort cleanup for browsers that deny sessionStorage access.
  }
}

export function reloadPrivacyModePage(): void {
  if (typeof window !== "undefined") window.location.reload();
}

const READ_METHODS = new Set(["get", "head", "options"]);
const ALLOWED_WRITE_PATHS = [
  /\/auth\/logout(?:$|\?)/,
  /\/auth\/refresh(?:$|\?)/,
  // The dashboard's mandatory release gate must remain dismissible after the
  // privacy toggle reloads the page. This only records acknowledgement state.
  /\/release-announcements\/acknowledge(?:$|\?)/,
];
const BLOCKED_READ_PATHS = [
  /\/notifications\/checkin-card\//,
];

export function isPrivacyRequestAllowed(
  method?: string,
  url?: string,
  responseType?: string,
): boolean {
  // JSON responses are redacted by the backend. Binary responses (notably
  // exports) cannot be redacted safely, so keep them out of recording mode.
  if (
    (responseType && !["json", "text"].includes(responseType))
    || /\/export(?:\/|$|\?)/.test(url || "")
    || BLOCKED_READ_PATHS.some((pattern) => pattern.test(url || ""))
  ) return false;
  if (READ_METHODS.has((method || "get").toLowerCase())) return true;
  return ALLOWED_WRITE_PATHS.some((pattern) => pattern.test(url || ""));
}

export class PrivacyModeReadOnlyError extends Error {
  constructor() {
    super("隐私演示模式已开启，当前只读且禁止导出；关闭后才能修改或下载原始数据");
    this.name = "PrivacyModeReadOnlyError";
  }
}
