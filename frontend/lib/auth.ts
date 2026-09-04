import { create } from "zustand";
import { clearAnnouncementSnooze } from "./release-announcement-snooze";
import { clearPrivacyMode } from "@/lib/privacy-mode";

/** Lightweight user info stored in auth state (from login/token response) */
export interface AuthUser {
  user_id: string;
  role: string;
  display_name: string;
}

interface AuthState {
  user: AuthUser | null;
  access_token: string | null;
  session_id: string | null;
  setAuth: (user: AuthUser, token: string, refresh: string) => void;
  clearAuth: (expectedSessionId?: string) => boolean;
  isAdmin: () => boolean;
  isOperator: () => boolean;
  isFinance: () => boolean;
}

interface SessionRecord { user: AuthUser; access_token: string; refresh_token: string }
const SESSION_PREFIX = "auth-session:v2:";
const REVOKED_PREFIX = "auth-session-revoked:v2:";
const sessionKey = (id: string) => `${SESSION_PREFIX}${id}`;
const revokedKey = (id: string) => `${REVOKED_PREFIX}${id}`;

function createSessionId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `session-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export const AUTH_SESSION_ID_KEY = "auth-session-id";

export function getPersistedAuthSessionId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(AUTH_SESSION_ID_KEY);
  } catch {
    return null;
  }
}

function readSession(id: string | null): SessionRecord | null {
  if (!id || typeof window === "undefined") return null;
  try {
    if (localStorage.getItem(revokedKey(id))) return null;
    const raw = localStorage.getItem(sessionKey(id));
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<SessionRecord>;
    return value.user && typeof value.access_token === "string" && typeof value.refresh_token === "string"
      ? value as SessionRecord : null;
  } catch { return null; }
}

export const getAuthSessionAccessToken = (id = getPersistedAuthSessionId()) => readSession(id)?.access_token ?? null;
export const getAuthSessionRefreshToken = (id = getPersistedAuthSessionId()) => readSession(id)?.refresh_token ?? null;
export function updateAuthSessionTokens(id: string, access_token: string, refresh_token: string): boolean {
  const current = readSession(id);
  if (!current) return false;
  try {
    localStorage.setItem(sessionKey(id), JSON.stringify({ ...current, access_token, refresh_token }));
    return !localStorage.getItem(revokedKey(id));
  } catch { return false; }
}

const initialId = getPersistedAuthSessionId();
const initial = readSession(initialId);

export const useAuthStore = create<AuthState>()((set, get) => ({
      user: initial?.user ?? null,
      access_token: initial?.access_token ?? null,
      session_id: initial ? initialId : null,
      setAuth: (user, access_token, refresh_token) => {
        clearPrivacyMode();
        clearAnnouncementSnooze(user.user_id);
        const session_id = createSessionId();
        localStorage.setItem(sessionKey(session_id), JSON.stringify({ user, access_token, refresh_token }));
        localStorage.setItem(AUTH_SESSION_ID_KEY, session_id);
        set({ user, access_token, session_id });
      },
      clearAuth: (expectedSessionId) => {
        const target = expectedSessionId ?? get().session_id ?? getPersistedAuthSessionId();
        if (!target) return true;
        const targetUserId = readSession(target)?.user.user_id
          ?? (get().session_id === target ? get().user?.user_id : undefined);
        localStorage.setItem(revokedKey(target), "1");
        localStorage.removeItem(sessionKey(target));
        if (targetUserId) clearAnnouncementSnooze(targetUserId);
        // Re-check after storage writes: another login can interleave here. A stale
        // logout must neither clear the newer account nor disable its privacy state.
        if (get().session_id === target) {
          clearPrivacyMode();
          set({ user: null, access_token: null, session_id: null });
        }
        return true;
      },
      isAdmin: () => get().user?.role === "admin",
      isOperator: () => ["admin", "operator"].includes(get().user?.role ?? ""),
      isFinance: () => ["admin", "finance"].includes(get().user?.role ?? ""),
}));
