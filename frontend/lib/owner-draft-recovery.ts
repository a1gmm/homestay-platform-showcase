import type { OwnerCaseDraft, OwnerDraft } from "@/features/content/owner/types";

const OWNER_DRAFT_RECOVERY_SCHEMA = "miniapp-content.owner-draft-recovery.v2";
const OWNER_DRAFT_RECOVERY_PREFIX = "miniapp-content:owner:draft-recovery:v1";
const OWNER_DRAFT_RECOVERY_QUARANTINE_PREFIX = "miniapp-content:owner:draft-recovery-quarantine:v1";
const MAX_RECOVERY_SERIALIZED_LENGTH = 512 * 1024;
const MAX_RECOVERY_RECORDS = 5;
const MAX_RECOVERY_STORAGE_ENTRIES = 128;
const RECORD_KEYS = ["baseRevision", "draft", "generation", "recordId", "schema", "scope", "writtenAt"];
const DRAFT_KEYS = ["cases", "revision", "video"];
const CASE_KEYS = [
  "caseId",
  "description",
  "detailImages",
  "isVisible",
  "primaryMediaId",
  "privacyConfirmed",
  "title",
];

let recordSequence = 0;
let lastRecordTimestamp = -1;

export interface OwnerDraftRecoveryRecord {
  schema: typeof OWNER_DRAFT_RECOVERY_SCHEMA;
  scope: string;
  recordId: string;
  generation: number;
  writtenAt: number;
  baseRevision: number;
  draft: OwnerDraft;
}

export type OwnerDraftRecoveryFailureReason = "capacity" | "invalid" | "quota" | "unavailable";

export interface OwnerDraftRecoveryFailure {
  reason: OwnerDraftRecoveryFailureReason;
  message: string;
}

export type OwnerDraftRecoveryFailureHandler = (failure: OwnerDraftRecoveryFailure) => void;

interface OwnerDraftRecoveryScan {
  readable: boolean;
  records: OwnerDraftRecoveryRecord[];
}

function hasExactKeys(value: Record<string, unknown>, allowedKeys: string[], optionalKeys: string[] = []): boolean {
  const keys = Object.keys(value);
  return keys.every((key) => allowedKeys.includes(key))
    && allowedKeys.every((key) => optionalKeys.includes(key) || Object.prototype.hasOwnProperty.call(value, key));
}

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function isBoundedString(value: unknown, min: number, max: number): value is string {
  if (typeof value !== "string") return false;
  const length = codePointLength(value);
  return length >= min && length <= max;
}

function isOwnerCaseDraft(value: unknown): value is OwnerCaseDraft {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  if (!hasExactKeys(candidate, CASE_KEYS, ["caseId"])) return false;
  if (candidate.caseId !== undefined && candidate.caseId !== null && !isBoundedString(candidate.caseId, 1, 24)) return false;
  // Recovery protects in-progress editor state, which may be temporarily
  // unsaveable (for example, while a title field is blank).
  if (!isBoundedString(candidate.title, 0, 120)) return false;
  if (candidate.description !== null && !isBoundedString(candidate.description, 0, 2000)) return false;
  if (typeof candidate.isVisible !== "boolean") return false;
  if (candidate.primaryMediaId !== null && !isBoundedString(candidate.primaryMediaId, 0, 24)) return false;
  if (!Array.isArray(candidate.detailImages) || candidate.detailImages.length > 20) return false;
  if (!candidate.detailImages.every((image) => {
    if (!image || typeof image !== "object" || Array.isArray(image)) return false;
    const item = image as Record<string, unknown>;
    return hasExactKeys(item, ["alt", "displayMediaId", "thumbnailMediaId"])
      && isBoundedString(item.alt, 1, 200)
      && isBoundedString(item.thumbnailMediaId, 1, 24)
      && isBoundedString(item.displayMediaId, 1, 24);
  })) return false;
  return typeof candidate.privacyConfirmed === "boolean";
}

function isOwnerDraft(value: unknown): value is OwnerDraft {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  const video = candidate.video;
  const validVideo = video === undefined || video === null || (
    typeof video === "object"
    && !Array.isArray(video)
    && hasExactKeys(video as Record<string, unknown>, ["alt", "mediaId", "posterMediaId"])
    && isBoundedString((video as Record<string, unknown>).mediaId, 1, 24)
    && isBoundedString((video as Record<string, unknown>).posterMediaId, 1, 24)
    && isBoundedString((video as Record<string, unknown>).alt, 1, 200)
  );
  return hasExactKeys(candidate, DRAFT_KEYS, ["video"])
    && Number.isInteger(candidate.revision)
    && (candidate.revision as number) >= 0
    && Array.isArray(candidate.cases)
    && candidate.cases.every(isOwnerCaseDraft)
    && validVideo;
}

function isRecoveryRecord(value: unknown, scope: string): value is OwnerDraftRecoveryRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  return hasExactKeys(candidate, RECORD_KEYS)
    && candidate.schema === OWNER_DRAFT_RECOVERY_SCHEMA
    && candidate.scope === scope
    && isBoundedString(candidate.recordId, 1, 128)
    && Number.isSafeInteger(candidate.generation)
    && (candidate.generation as number) >= 0
    && Number.isInteger(candidate.writtenAt)
    && (candidate.writtenAt as number) >= 0
    && Number.isInteger(candidate.baseRevision)
    && (candidate.baseRevision as number) >= 0
    && isOwnerDraft(candidate.draft)
    && candidate.draft.revision === candidate.baseRevision;
}

function nextRecordIdentity(): { recordId: string; generation: number; writtenAt: number } {
  const writtenAt = Date.now();
  if (writtenAt === lastRecordTimestamp) recordSequence += 1;
  else {
    lastRecordTimestamp = writtenAt;
    recordSequence = 0;
  }
  const recordId = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${writtenAt.toString(36)}-${recordSequence.toString(36)}-${Math.random().toString(36).slice(2)}`;
  return { recordId, generation: writtenAt * 1000 + recordSequence, writtenAt };
}

function buildRecord(scope: string, draft: OwnerDraft): OwnerDraftRecoveryRecord {
  const identity = nextRecordIdentity();
  return {
    schema: OWNER_DRAFT_RECOVERY_SCHEMA,
    scope,
    ...identity,
    baseRevision: draft.revision,
    draft,
  };
}

function recoveryFailure(
  reason: OwnerDraftRecoveryFailureReason,
  onFailure?: OwnerDraftRecoveryFailureHandler,
): void {
  const messages: Record<OwnerDraftRecoveryFailureReason, string> = {
    capacity: "已有 5 份未解决的本地恢复副本，已停止创建更多备份。请先选择或放弃已有副本。",
    invalid: "当前编辑内容结构异常，本地恢复副本未能保存。请保持页面打开并检查内容。",
    quota: "浏览器存储空间不足，本地恢复副本未能保存。请保持页面打开并释放浏览器存储空间。",
    unavailable: "浏览器存储暂不可用，本地恢复副本未能保存。请保持页面打开并稍后重试。",
  };
  onFailure?.({ reason, message: messages[reason] });
}

function isQuotaFailure(error: unknown): boolean {
  return error instanceof DOMException
    && (error.name === "QuotaExceededError" || error.name === "NS_ERROR_DOM_QUOTA_REACHED");
}

function removeRecoveryKey(key: string, onFailure?: OwnerDraftRecoveryFailureHandler): boolean {
  try {
    window.localStorage.removeItem(key);
    return true;
  } catch {
    recoveryFailure("unavailable", onFailure);
    return false;
  }
}

function parseRecoveryRecord(raw: string, scope: string): OwnerDraftRecoveryRecord | null {
  if (raw.length > MAX_RECOVERY_SERIALIZED_LENGTH) return null;
  try {
    const record: unknown = JSON.parse(raw);
    return isRecoveryRecord(record, scope) ? record : null;
  } catch {
    return null;
  }
}

export function ownerDraftRecoveryKey(scope: string, recordId?: string): string {
  const namespace = `${OWNER_DRAFT_RECOVERY_PREFIX}:${scope}`;
  return recordId ? `${namespace}:${recordId}` : namespace;
}

function ownerDraftRecoveryQuarantineKey(scope: string, recordId: string): string {
  return `${OWNER_DRAFT_RECOVERY_QUARANTINE_PREFIX}:${encodeURIComponent(scope)}:${recordId}`;
}

function quarantineRecoveryRecord(
  scope: string,
  record: OwnerDraftRecoveryRecord,
  activeKey: string,
  raw: string,
  onFailure?: OwnerDraftRecoveryFailureHandler,
): boolean {
  try {
    window.localStorage.setItem(ownerDraftRecoveryQuarantineKey(scope, record.recordId), raw);
    window.localStorage.removeItem(activeKey);
    return true;
  } catch (error) {
    recoveryFailure(isQuotaFailure(error) ? "quota" : "unavailable", onFailure);
    return false;
  }
}

function storeRecoveryRecord(
  scope: string,
  draft: OwnerDraft,
  onFailure?: OwnerDraftRecoveryFailureHandler,
): OwnerDraftRecoveryRecord | null {
  const record = buildRecord(scope, draft);
  try {
    const serialized = JSON.stringify(record);
    if (serialized.length > MAX_RECOVERY_SERIALIZED_LENGTH) {
      recoveryFailure("invalid", onFailure);
      return null;
    }
    window.localStorage.setItem(ownerDraftRecoveryKey(scope, record.recordId), serialized);
    return record;
  } catch (error) {
    recoveryFailure(isQuotaFailure(error) ? "quota" : "unavailable", onFailure);
    return null;
  }
}

function storeRecoveryRecordWithinCapacity(
  scope: string,
  draft: OwnerDraft,
  onFailure?: OwnerDraftRecoveryFailureHandler,
  supersededRecordId?: string,
): OwnerDraftRecoveryRecord | null {
  const record = storeRecoveryRecord(scope, draft, onFailure);
  if (!record) return null;
  const postWrite = scanOwnerDraftRecovery(scope, onFailure);
  const retained = postWrite.records.filter((candidate) => candidate.recordId !== supersededRecordId);
  if (!postWrite.readable || !retained.some((candidate) => candidate.recordId === record.recordId)) {
    removeRecoveryKey(ownerDraftRecoveryKey(scope, record.recordId), onFailure);
    return null;
  }
  if (retained.length > MAX_RECOVERY_RECORDS) {
    removeRecoveryKey(ownerDraftRecoveryKey(scope, record.recordId), onFailure);
    recoveryFailure("capacity", onFailure);
    return null;
  }
  return record;
}

function scanOwnerDraftRecovery(
  scope: string,
  onFailure?: OwnerDraftRecoveryFailureHandler,
): OwnerDraftRecoveryScan {
  if (typeof window === "undefined") {
    recoveryFailure("unavailable", onFailure);
    return { readable: false, records: [] };
  }
  const namespace = ownerDraftRecoveryKey(scope);
  const recordPrefix = `${namespace}:`;
  const candidates: Array<{ key: string; raw: string }> = [];
  try {
    if (window.localStorage.length > MAX_RECOVERY_STORAGE_ENTRIES) {
      recoveryFailure("capacity", onFailure);
      return { readable: false, records: [] };
    }
    const legacyRaw = window.localStorage.getItem(namespace);
    if (legacyRaw !== null) candidates.push({ key: namespace, raw: legacyRaw });
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (!key?.startsWith(recordPrefix)) continue;
      const raw = window.localStorage.getItem(key);
      if (raw !== null) candidates.push({ key, raw });
    }
  } catch {
    recoveryFailure("unavailable", onFailure);
    return { readable: false, records: [] };
  }

  const records: OwnerDraftRecoveryRecord[] = [];
  let quarantinedExcess = false;
  candidates.forEach(({ key, raw }) => {
    const record = parseRecoveryRecord(raw, scope);
    if (!record || key !== ownerDraftRecoveryKey(scope, record.recordId)) {
      removeRecoveryKey(key, onFailure);
      return;
    }
    if (records.length >= MAX_RECOVERY_RECORDS) {
      quarantinedExcess = true;
      quarantineRecoveryRecord(scope, record, key, raw, onFailure);
      return;
    }
    records.push(record);
  });
  if (quarantinedExcess) recoveryFailure("capacity", onFailure);
  // Stable presentation order only. A random record ID is never interpreted
  // as recency or used to choose a recovery automatically.
  records.sort((left, right) => left.recordId.localeCompare(right.recordId));
  return { readable: true, records };
}

export function readOwnerDraftRecoveryCandidates(
  scope: string,
  onFailure?: OwnerDraftRecoveryFailureHandler,
): OwnerDraftRecoveryRecord[] {
  return scanOwnerDraftRecovery(scope, onFailure).records;
}

export function readOwnerDraftRecovery(
  scope: string,
  onFailure?: OwnerDraftRecoveryFailureHandler,
): OwnerDraftRecoveryRecord | null {
  const records = readOwnerDraftRecoveryCandidates(scope, onFailure);
  return records.length === 1 ? records[0] : null;
}

export function writeOwnerDraftRecovery(
  scope: string,
  draft: OwnerDraft,
  onFailure?: OwnerDraftRecoveryFailureHandler,
): OwnerDraftRecoveryRecord | null {
  if (typeof window === "undefined" || !isOwnerDraft(draft)) {
    recoveryFailure("invalid", onFailure);
    return null;
  }
  const scan = scanOwnerDraftRecovery(scope, onFailure);
  if (!scan.readable) return null;
  if (scan.records.length >= MAX_RECOVERY_RECORDS) {
    recoveryFailure("capacity", onFailure);
    return null;
  }
  return storeRecoveryRecordWithinCapacity(scope, draft, onFailure);
}

export function replaceOwnerDraftRecovery(
  scope: string,
  expectedRecordId: string,
  draft: OwnerDraft,
  onFailure?: OwnerDraftRecoveryFailureHandler,
): OwnerDraftRecoveryRecord | null {
  if (typeof window === "undefined" || !isOwnerDraft(draft)) {
    recoveryFailure("invalid", onFailure);
    return null;
  }
  const expectedKey = ownerDraftRecoveryKey(scope, expectedRecordId);
  const scan = scanOwnerDraftRecovery(scope, onFailure);
  if (!scan.readable) return null;
  const current = scan.records.find((record) => record.recordId === expectedRecordId) || null;
  if (current?.recordId !== expectedRecordId) {
    if (scan.records.length >= MAX_RECOVERY_RECORDS) {
      recoveryFailure("capacity", onFailure);
      return null;
    }
    return storeRecoveryRecordWithinCapacity(scope, draft, onFailure);
  }
  const unresolvedOthers = scan.records.filter((record) => record.recordId !== expectedRecordId);
  if (unresolvedOthers.length >= MAX_RECOVERY_RECORDS) {
    recoveryFailure("capacity", onFailure);
    return null;
  }
  const replacement = storeRecoveryRecordWithinCapacity(
    scope,
    draft,
    onFailure,
    expectedRecordId,
  );
  if (!replacement) return null;
  removeRecoveryKey(expectedKey, onFailure);
  return replacement;
}

export function clearOwnerDraftRecovery(
  scope: string,
  expectedRecordId: string,
  onFailure?: OwnerDraftRecoveryFailureHandler,
): boolean {
  if (typeof window === "undefined") return false;
  const expectedKey = ownerDraftRecoveryKey(scope, expectedRecordId);
  try {
    const raw = window.localStorage.getItem(expectedKey);
    if (raw === null) return true;
    const current = raw ? parseRecoveryRecord(raw, scope) : null;
    if (current?.recordId !== expectedRecordId) return false;
    window.localStorage.removeItem(expectedKey);
    return true;
  } catch {
    recoveryFailure("unavailable", onFailure);
    return false;
  }
}

export function clearOwnerDraftRecoveries(
  scope: string,
  expectedRecordIds: Iterable<string>,
  onFailure?: OwnerDraftRecoveryFailureHandler,
): boolean {
  let cleared = true;
  Array.from(new Set(expectedRecordIds)).sort().forEach((recordId) => {
    if (!clearOwnerDraftRecovery(scope, recordId, onFailure)) cleared = false;
  });
  return cleared;
}
