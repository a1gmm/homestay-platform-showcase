const OWNER_VIDEO_DRAFT_SCHEMA = "miniapp-content.owner-video-draft.v1";
const OWNER_VIDEO_DRAFT_PREFIX = "miniapp-content:owner:video-draft:v1";

export interface PendingOwnerVideoDraft {
  mediaId: string | null;
  posterMediaId: string | null;
  alt: string;
}

export interface OwnerVideoDraftRecovery {
  schema: typeof OWNER_VIDEO_DRAFT_SCHEMA;
  scope: string;
  baseRevision: number;
  writtenAt: number;
  video: PendingOwnerVideoDraft;
}

function key(scope: string): string {
  return `${OWNER_VIDEO_DRAFT_PREFIX}:${encodeURIComponent(scope)}`;
}

function isMediaId(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && value.length > 0 && value.length <= 24);
}

function isRecovery(value: unknown, scope: string): value is OwnerVideoDraftRecovery {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  const video = candidate.video;
  if (!video || typeof video !== "object" || Array.isArray(video)) return false;
  const pending = video as Record<string, unknown>;
  return candidate.schema === OWNER_VIDEO_DRAFT_SCHEMA
    && candidate.scope === scope
    && Number.isInteger(candidate.baseRevision)
    && (candidate.baseRevision as number) >= 0
    && Number.isInteger(candidate.writtenAt)
    && isMediaId(pending.mediaId)
    && isMediaId(pending.posterMediaId)
    && typeof pending.alt === "string"
    && pending.alt.length > 0
    && pending.alt.length <= 200
    && Boolean(pending.mediaId || pending.posterMediaId);
}

export function readOwnerVideoDraftRecovery(scope: string): OwnerVideoDraftRecovery | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(key(scope));
    if (!raw || raw.length > 4096) return null;
    const parsed: unknown = JSON.parse(raw);
    return isRecovery(parsed, scope) ? parsed : null;
  } catch {
    return null;
  }
}

export function writeOwnerVideoDraftRecovery(
  scope: string,
  baseRevision: number,
  video: PendingOwnerVideoDraft,
): boolean {
  if (typeof window === "undefined") return false;
  if (!video.mediaId && !video.posterMediaId) return clearOwnerVideoDraftRecovery(scope);
  const record: OwnerVideoDraftRecovery = {
    schema: OWNER_VIDEO_DRAFT_SCHEMA,
    scope,
    baseRevision,
    writtenAt: Date.now(),
    video,
  };
  try {
    window.localStorage.setItem(key(scope), JSON.stringify(record));
    return true;
  } catch {
    return false;
  }
}

export function clearOwnerVideoDraftRecovery(scope: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    window.localStorage.removeItem(key(scope));
    return true;
  } catch {
    return false;
  }
}
