const SNOOZE_KEY_PREFIX = "release-announcements:snoozed:";

const storageKey = (userId: string) =>
  `${SNOOZE_KEY_PREFIX}${encodeURIComponent(userId)}`;

export function getSnoozedAnnouncementIds(userId: string): Set<string> {
  if (typeof window === "undefined") return new Set();

  try {
    const raw = window.sessionStorage.getItem(storageKey(userId));
    if (raw === null) return new Set();

    const value: unknown = JSON.parse(raw);
    if (
      !Array.isArray(value) ||
      value.some((item) => typeof item !== "string" || item.trim().length === 0)
    ) {
      return new Set();
    }
    return new Set(value);
  } catch {
    return new Set();
  }
}

export function snoozeAnnouncements(userId: string, ids: string[]): void {
  if (typeof window === "undefined") return;

  try {
    const snoozed = getSnoozedAnnouncementIds(userId);
    for (const id of ids) {
      if (id.trim().length > 0) snoozed.add(id);
    }
    window.sessionStorage.setItem(storageKey(userId), JSON.stringify(Array.from(snoozed)));
  } catch {
    // Snoozing is a best-effort session preference and must not block the workspace.
  }
}

export function clearAnnouncementSnooze(userId: string): void {
  if (typeof window === "undefined") return;

  try {
    window.sessionStorage.removeItem(storageKey(userId));
  } catch {
    // Storage can be unavailable in privacy modes; authentication must still proceed.
  }
}
