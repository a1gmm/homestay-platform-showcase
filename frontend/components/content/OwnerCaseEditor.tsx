"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDownOutlined, ArrowUpOutlined, EyeOutlined, PlusOutlined } from "@ant-design/icons";
import type {
  OwnerCaseDraft,
  OwnerDraft,
  OwnerDraftVideo,
  OwnerDraftPreview,
  OwnerImageUploadPair,
  PublishOwnerInput,
  RollbackOwnerInput,
} from "@/features/content/owner/types";
import type { OwnerRelease, PublishJob, PublishJobAccepted } from "@/features/content/types";
import { isContentConflict } from "@/features/content/types";
import { useUnsavedNavigationGuard } from "@/hooks/useUnsavedNavigationGuard";
import { useModalDialog } from "@/hooks/useModalDialog";
import {
  buildOwnerDraftPayload,
  diffOwnerDraft,
  getOwnerPublishIssues,
  getPublishFailurePresentation,
  moveOwnerCase,
  replaceOwnerCaseMedia,
  type OwnerPublishIssue,
} from "@/lib/miniapp-content";
import {
  clearOwnerDraftRecoveries,
  clearOwnerDraftRecovery,
  readOwnerDraftRecoveryCandidates,
  replaceOwnerDraftRecovery,
  type OwnerDraftRecoveryFailure,
  type OwnerDraftRecoveryRecord,
  writeOwnerDraftRecovery,
} from "@/lib/owner-draft-recovery";
import {
  clearOwnerVideoDraftRecovery,
  readOwnerVideoDraftRecovery,
  writeOwnerVideoDraftRecovery,
} from "@/lib/owner-video-draft-recovery";
import { MediaUploader, type MediaUploadHandler, type UploadedContentMedia } from "./MediaUploader";
import { ContentEditorShell, type ContentEditorAction, type ContentPreviewWidth } from "./ContentEditorShell";
import { MobileOwnerPreview } from "./MobileOwnerPreview";
import { PublishDialog } from "./PublishDialog";
import { ReleaseHistoryDrawer } from "./ReleaseHistoryDrawer";
import type { OwnerSaveState } from "./SaveState";
import styles from "./owner-content.module.css";

export interface OwnerCaseEditorProps {
  initialDraft: OwnerDraft;
  role: string;
  publishedVersion: string | null;
  onSave: (input: OwnerDraft) => Promise<OwnerDraft>;
  onReload: () => Promise<OwnerDraft>;
  onPreview: (revision: number) => Promise<OwnerDraftPreview>;
  onUploadImage: MediaUploadHandler<UploadedContentMedia>;
  onUploadImagePair: MediaUploadHandler<OwnerImageUploadPair>;
  onUploadVideo: MediaUploadHandler<UploadedContentMedia>;
  onRequestMediaUrl: (mediaId: string) => Promise<{ url: string; expiresSeconds: number }>;
  onPublish: (input: PublishOwnerInput) => Promise<PublishJobAccepted>;
  onJobStarted: (jobId: string) => void;
  publishJob: PublishJob | null;
  releases: OwnerRelease[];
  onRollback: (input: RollbackOwnerInput) => Promise<PublishJobAccepted>;
  onConflictCleared: () => void;
  onNavigate?: (href: string) => void;
  activeJobId?: string | null;
  releasesStatus?: "loading" | "success" | "error";
  onRetryReleases?: () => void;
  publishTrackingStatus?: "idle" | "loading" | "success" | "error";
  publishTrackingError?: string | null;
  onRetryPublishTracking?: () => void;
  recoveryScope?: string;
}

type EditorCase = OwnerCaseDraft & { editorCaseId: string };
type EditorVideo = {
  mediaId: string | null;
  posterMediaId: string | null;
  alt: string;
};

let clientCaseSequence = 0;
const editorCaseId = (item: OwnerCaseDraft) => item.caseId || `owner-case-local-${++clientCaseSequence}`;
const toEditorCases = (items: OwnerCaseDraft[], prior: EditorCase[] = []): EditorCase[] => items.map((item, index) => ({
  ...item,
  editorCaseId: prior.find((candidate) => candidate.caseId && candidate.caseId === item.caseId)?.editorCaseId
    || prior[index]?.editorCaseId
    || item.caseId
    || editorCaseId(item),
}));

const blankCase = (): EditorCase => ({
  editorCaseId: editorCaseId({} as OwnerCaseDraft),
  title: "未命名房源",
  description: null,
  isVisible: false,
  primaryMediaId: null,
  detailImages: [],
  privacyConfirmed: false,
});

type OwnerActionIntent =
  | { operation: "publish"; revision: number; idempotencyKey: string }
  | { operation: "rollback"; version: string; idempotencyKey: string };

type RequestRecovery = { message: string; intent: OwnerActionIntent };

function sameDraft(left: OwnerDraft, right: OwnerDraft): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function toEditorVideo(video?: OwnerDraftVideo | null): EditorVideo {
  return {
    mediaId: video?.mediaId || null,
    posterMediaId: video?.posterMediaId || null,
    alt: video?.alt || "整理房源的工作过程",
  };
}

function completeVideo(video: EditorVideo): OwnerDraftVideo | null {
  return video.mediaId && video.posterMediaId
    ? { mediaId: video.mediaId, posterMediaId: video.posterMediaId, alt: video.alt }
    : null;
}

function incompleteVideoMessage(video: EditorVideo): string | null {
  if (video.mediaId && !video.posterMediaId) {
    return "新视频已上传，还需要上传新封面后才能保存和发布";
  }
  if (!video.mediaId && video.posterMediaId) {
    return "视频封面已上传，还需要上传展示视频后才能保存和发布";
  }
  return null;
}

type EditorPublishIssue = {
  message: string;
  field: OwnerPublishIssue["field"] | "video";
  caseIndex?: number;
};

function idempotencyKey(prefix: string) {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function acceptedJob(accepted: PublishJobAccepted, operation: "publish" | "rollback"): PublishJob {
  return {
    jobId: accepted.jobId,
    channel: accepted.channel,
    operation,
    status: accepted.status,
    expectedRevision: accepted.expectedRevision ?? null,
    targetVersion: accepted.targetVersion ?? null,
    progressStage: accepted.status === "queued" ? "queued" : null,
    progressPercent: 0,
    errorCode: null,
  };
}

export function OwnerCaseEditor(props: OwnerCaseEditorProps) {
  const {
    initialDraft,
    role,
    publishedVersion,
    onSave,
    onReload,
    onPreview,
    onUploadImage,
    onUploadImagePair,
    onUploadVideo,
    onRequestMediaUrl,
    onPublish,
    onJobStarted,
    publishJob,
    releases,
    onRollback,
    onConflictCleared,
    onNavigate,
    activeJobId,
    releasesStatus = "success",
    onRetryReleases,
    publishTrackingStatus = "success",
    publishTrackingError,
    onRetryPublishTracking,
    recoveryScope = "default",
  } = props;
  const isAdmin = role === "admin";
  const [cases, setCases] = useState<EditorCase[]>(() => toEditorCases(initialDraft.cases));
  const [revision, setRevision] = useState(initialDraft.revision);
  const [video, setVideo] = useState<EditorVideo>(() => toEditorVideo(initialDraft.video));
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [saveState, setSaveState] = useState<OwnerSaveState>("saved");
  const [conflictLocal, setConflictLocal] = useState<OwnerDraft | null>(null);
  const [conflictServer, setConflictServer] = useState<OwnerDraft | null>(null);
  const [conflictDialogOpen, setConflictDialogOpen] = useState(false);
  const [preview, setPreview] = useState<OwnerDraftPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewExpired, setPreviewExpired] = useState(false);
  const [weakNetwork, setWeakNetwork] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [requestRecovery, setRequestRecovery] = useState<RequestRecovery | null>(null);
  const [localJob, setLocalJob] = useState<PublishJob | null>(null);
  const [operation, setOperation] = useState<"preview" | "publish" | "history" | "rollback" | null>(null);
  const [videoStatus, setVideoStatus] = useState<string | null>(null);
  const [primaryPreviewUrl, setPrimaryPreviewUrl] = useState<string | null>(null);
  const [primaryPreviewError, setPrimaryPreviewError] = useState(false);
  const [activeUploads, setActiveUploads] = useState<Set<string>>(() => new Set());
  const [draftRecoveryState, setDraftRecoveryState] = useState<"choice" | "restored" | "conflict" | null>(null);
  const [recoveryCandidates, setRecoveryCandidates] = useState<OwnerDraftRecoveryRecord[]>([]);
  const [recoveryWarning, setRecoveryWarning] = useState<string | null>(null);
  const casesRef = useRef(cases);
  const videoRef = useRef(video);
  const revisionRef = useRef(revision);
  const serverConfirmedDraftRef = useRef(initialDraft);
  const saveStateRef = useRef(saveState);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const savePromiseRef = useRef<Promise<OwnerDraft> | null>(null);
  const conflictPrimaryRef = useRef<HTMLButtonElement>(null);
  const navigationPrimaryRef = useRef<HTMLButtonElement>(null);
  const navigationDialogRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);
  const activeUploadsRef = useRef(activeUploads);
  const videoDraftPendingRef = useRef(false);
  const requestIntentRef = useRef<OwnerActionIntent | null>(null);
  const operationRef = useRef<typeof operation>(null);
  const requestMediaUrlRef = useRef(onRequestMediaUrl);
  const localChangeEpochRef = useRef(0);
  const recoveryRecordRef = useRef<OwnerDraftRecoveryRecord | null>(null);
  const recoveryResolutionIdsRef = useRef<Set<string>>(new Set());
  const hydrationRef = useRef<{ scope: string; recordIds: string[]; localChangeEpoch: number } | null>(null);

  const updateSaveState = (state: OwnerSaveState) => {
    saveStateRef.current = state;
    setSaveState(state);
  };
  useEffect(() => {
    requestMediaUrlRef.current = onRequestMediaUrl;
  }, [onRequestMediaUrl]);
  const replaceCases = (next: EditorCase[]) => {
    casesRef.current = next;
    setCases(next);
  };
  const replaceVideo = (next: EditorVideo) => {
    videoRef.current = next;
    setVideo(next);
  };
  const handleRecoveryFailure = useCallback((failure: OwnerDraftRecoveryFailure) => {
    if (mountedRef.current) setRecoveryWarning(failure.message);
  }, []);
  const rememberRecoveryReplacement = useCallback((previousRecordId: string, record: OwnerDraftRecoveryRecord) => {
    recoveryResolutionIdsRef.current.add(previousRecordId);
    recoveryResolutionIdsRef.current.add(record.recordId);
    recoveryRecordRef.current = record;
  }, []);
  const clearKnownRecoveryRecords = useCallback(() => {
    const cleared = clearOwnerDraftRecoveries(
      recoveryScope,
      recoveryResolutionIdsRef.current,
      handleRecoveryFailure,
    );
    if (cleared) {
      recoveryResolutionIdsRef.current.clear();
      setRecoveryWarning(null);
    }
    return cleared;
  }, [handleRecoveryFailure, recoveryScope]);
  const persistCurrentDraft = useCallback(() => {
    const currentVideo = videoRef.current;
    if (videoDraftPendingRef.current && (currentVideo.mediaId || currentVideo.posterMediaId)) {
      if (!writeOwnerVideoDraftRecovery(recoveryScope, revisionRef.current, currentVideo)) {
        handleRecoveryFailure({ reason: "unavailable", message: "未完成的视频更换无法写入本地恢复记录，请保持页面打开。" });
      }
    }
    const draft = buildOwnerDraftPayload(
      casesRef.current,
      revisionRef.current,
      completeVideo(videoRef.current),
    );
    const ownedRecovery = recoveryRecordRef.current;
    const record = ownedRecovery
      ? replaceOwnerDraftRecovery(recoveryScope, ownedRecovery.recordId, draft, handleRecoveryFailure)
      : writeOwnerDraftRecovery(recoveryScope, draft, handleRecoveryFailure);
    if (record) {
      if (ownedRecovery) rememberRecoveryReplacement(ownedRecovery.recordId, record);
      else {
        recoveryRecordRef.current = record;
        recoveryResolutionIdsRef.current.add(record.recordId);
      }
    }
    return record;
  }, [handleRecoveryFailure, recoveryScope, rememberRecoveryReplacement]);

  useEffect(() => {
    const offline = () => setWeakNetwork(true);
    const online = () => setWeakNetwork(false);
    window.addEventListener("offline", offline);
    window.addEventListener("online", online);
    return () => {
      window.removeEventListener("offline", offline);
      window.removeEventListener("online", online);
    };
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      const hasPendingDraft = saveStateRef.current !== "saved"
        || !!timerRef.current
        || !!savePromiseRef.current;
      if (hasPendingDraft) persistCurrentDraft();
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      mountedRef.current = false;
    };
  }, [persistCurrentDraft]);

  useEffect(() => {
    if (hydrationRef.current?.scope === recoveryScope) return;
    const hydrationEpoch = localChangeEpochRef.current;
    const candidates = readOwnerDraftRecoveryCandidates(recoveryScope, handleRecoveryFailure);
    hydrationRef.current = {
      scope: recoveryScope,
      recordIds: candidates.map((record) => record.recordId),
      localChangeEpoch: hydrationEpoch,
    };
    recoveryResolutionIdsRef.current = new Set(candidates.map((record) => record.recordId));
    if (candidates.length > 1) {
      recoveryRecordRef.current = null;
      setRecoveryCandidates(candidates);
      updateSaveState("conflict");
      setDraftRecoveryState("choice");
      return;
    }
    const recovery = candidates[0] || null;
    recoveryRecordRef.current = recovery;
    if (!recovery) return;
    if (localChangeEpochRef.current !== hydrationEpoch) return;
    replaceCases(toEditorCases(recovery.draft.cases, casesRef.current));
    replaceVideo(toEditorVideo(recovery.draft.video));
    revisionRef.current = recovery.baseRevision;
    setRevision(recovery.baseRevision);
    if (recovery.baseRevision === initialDraft.revision) {
      updateSaveState("failed");
      setDraftRecoveryState("restored");
      return;
    }
    setConflictLocal(recovery.draft);
    setConflictServer(initialDraft);
    updateSaveState("conflict");
    setDraftRecoveryState("conflict");
  }, [handleRecoveryFailure, initialDraft, recoveryScope]);

  useEffect(() => {
    const pendingVideo = readOwnerVideoDraftRecovery(recoveryScope);
    if (!pendingVideo) return;
    if (pendingVideo.baseRevision !== initialDraft.revision || saveStateRef.current === "conflict") {
      setRecoveryWarning("检测到另一版本中未完成的视频更换。为避免覆盖当前内容，请重新上传视频和封面。");
      return;
    }
    replaceVideo(pendingVideo.video);
    videoDraftPendingRef.current = true;
    updateSaveState("failed");
    setDraftRecoveryState((current) => current || "restored");
    setVideoStatus(pendingVideo.video.mediaId && !pendingVideo.video.posterMediaId
      ? "已恢复尚未完成的视频更换；请补充新封面"
      : !pendingVideo.video.mediaId && pendingVideo.video.posterMediaId
        ? "已恢复尚未完成的视频更换；请补充展示视频"
        : "已恢复尚未保存的视频更换，请保存修改");
  }, [initialDraft.revision, recoveryScope]);

  const setUploadActive = (slot: string, active: boolean) => {
    if (active) armHistoryGuard();
    setActiveUploads((current) => {
      const next = new Set(current);
      if (active) next.add(slot);
      else next.delete(slot);
      activeUploadsRef.current = next;
      return next;
    });
  };

  useEffect(() => {
    if (publishJob) {
      setLocalJob(publishJob);
      setHistoryOpen(false);
      setPublishOpen(true);
    }
  }, [publishJob]);

  useEffect(() => {
    if (!conflictDialogOpen) return;
    conflictPrimaryRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setConflictDialogOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [conflictDialogOpen]);

  useEffect(() => {
    if (activeJobId) {
      setHistoryOpen(false);
      setPublishOpen(true);
    }
  }, [activeJobId]);

  useEffect(() => {
    if (!requestRecovery) return;
    setPublishOpen(false);
    setHistoryOpen(false);
  }, [requestRecovery]);

  const performSave = useCallback((saveCases?: EditorCase[], saveRevision?: number, resolveConflict = false): Promise<OwnerDraft> => {
    if (!mountedRef.current) return Promise.reject(new Error("编辑器已离开"));
    if (saveStateRef.current === "conflict" && !resolveConflict) return Promise.reject(new Error("请先解决内容冲突"));
    const videoProblem = incompleteVideoMessage(videoRef.current);
    if (videoProblem) return Promise.reject(new Error(videoProblem));
    if (savePromiseRef.current) return savePromiseRef.current;
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const snapshot = saveCases || casesRef.current;
    const videoSnapshot = videoRef.current;
    const token = saveRevision ?? revisionRef.current;
    const payload = buildOwnerDraftPayload(snapshot, token, completeVideo(videoSnapshot));
    let recoveryToAcknowledge = recoveryRecordRef.current;
    const ownedRecoveryRecordId = recoveryToAcknowledge?.recordId || null;
    if (recoveryToAcknowledge && !sameDraft(recoveryToAcknowledge.draft, payload)) {
      const replacement = replaceOwnerDraftRecovery(
        recoveryScope,
        recoveryToAcknowledge.recordId,
        payload,
        handleRecoveryFailure,
      );
      if (replacement) {
        rememberRecoveryReplacement(recoveryToAcknowledge.recordId, replacement);
      }
      recoveryToAcknowledge = replacement;
    }
    const recoveryRecordId = recoveryToAcknowledge && sameDraft(recoveryToAcknowledge.draft, payload)
      ? recoveryToAcknowledge.recordId
      : ownedRecoveryRecordId;
    updateSaveState("saving");
    const promise = onSave(payload)
      .then((saved) => {
        serverConfirmedDraftRef.current = saved;
        const completedVideo = completeVideo(videoSnapshot);
        if (completedVideo && JSON.stringify(saved.video || null) === JSON.stringify(completedVideo)) {
          if (clearOwnerVideoDraftRecovery(recoveryScope)) videoDraftPendingRef.current = false;
        }
        if (recoveryRecordId) {
          const cleared = clearOwnerDraftRecovery(recoveryScope, recoveryRecordId, handleRecoveryFailure);
          if (cleared) recoveryResolutionIdsRef.current.delete(recoveryRecordId);
          if (cleared && recoveryRecordRef.current?.recordId === recoveryRecordId) {
            recoveryRecordRef.current = null;
          }
        }
        if (!mountedRef.current) return saved;
        revisionRef.current = saved.revision;
        setRevision(saved.revision);
        setConflictLocal(null);
        setConflictServer(null);
        onConflictCleared();
        if (casesRef.current === snapshot && videoRef.current === videoSnapshot) {
          if (recoveryRecordId) clearKnownRecoveryRecords();
          recoveryRecordRef.current = null;
          setRecoveryCandidates([]);
          setDraftRecoveryState(null);
          replaceCases(toEditorCases(saved.cases, snapshot));
          replaceVideo(toEditorVideo(saved.video));
          updateSaveState("saved");
        } else {
          updateSaveState("saving");
          if (mountedRef.current) timerRef.current = setTimeout(() => void performSave().catch(() => undefined), 800);
        }
        return saved;
      })
      .catch((error) => {
        if (!mountedRef.current) throw error;
        if (isContentConflict(error)) {
          if (timerRef.current) {
            clearTimeout(timerRef.current);
            timerRef.current = null;
          }
          const local = buildOwnerDraftPayload(
            casesRef.current,
            token,
            completeVideo(videoRef.current),
          );
          setConflictLocal(local);
          updateSaveState("conflict");
        } else {
          updateSaveState("failed");
        }
        throw error;
      })
      .finally(() => {
        savePromiseRef.current = null;
      });
    savePromiseRef.current = promise;
    return promise;
  }, [clearKnownRecoveryRecords, handleRecoveryFailure, onConflictCleared, onSave, recoveryScope, rememberRecoveryReplacement]);

  const scheduleSave = (next: EditorCase[], nextVideo = videoRef.current) => {
    if (!mountedRef.current) return;
    localChangeEpochRef.current += 1;
    const ownedRecovery = recoveryRecordRef.current;
    if (ownedRecovery) {
      const replacement = replaceOwnerDraftRecovery(
        recoveryScope,
        ownedRecovery.recordId,
        buildOwnerDraftPayload(next, revisionRef.current, completeVideo(nextVideo)),
        handleRecoveryFailure,
      );
      if (replacement) {
        rememberRecoveryReplacement(ownedRecovery.recordId, replacement);
      }
    }
    armHistoryGuard();
    replaceCases(next);
    replaceVideo(nextVideo);
    if (saveStateRef.current === "conflict") return;
    updateSaveState("saving");
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      void performSave().catch(() => undefined);
    }, 800);
  };

  const flushSave = async () => {
    while (true) {
      const videoProblem = incompleteVideoMessage(videoRef.current);
      if (videoProblem) throw new Error(videoProblem);
      if (saveStateRef.current === "conflict") {
        throw new Error("请先解决内容冲突");
      }
      if (
        saveStateRef.current === "saved"
        && !savePromiseRef.current
        && !timerRef.current
      ) {
        return buildOwnerDraftPayload(
          casesRef.current,
          revisionRef.current,
          completeVideo(videoRef.current),
        );
      }
      await (savePromiseRef.current || performSave());
    }
  };

  const hasPendingNavigationWork = useCallback(() => saveStateRef.current !== "saved"
    || !!timerRef.current
    || !!savePromiseRef.current
    || activeUploadsRef.current.size > 0
    || !!incompleteVideoMessage(videoRef.current), []);
  const navigateFromEditor = useCallback((href: string) => {
    if (onNavigate) onNavigate(href);
    else window.location.assign(href);
  }, [onNavigate]);
  const {
    pendingNavigation,
    navigationConfirming,
    confirmNavigation: confirmPendingNavigation,
    cancelNavigation,
    armHistoryGuard,
  } = useUnsavedNavigationGuard({
    hasPendingWork: hasPendingNavigationWork,
    saveBeforeLeave: flushSave,
    persistBeforeUnload: persistCurrentDraft,
    navigateTo: navigateFromEditor,
  });
  useModalDialog({
    open: !!pendingNavigation && !conflictDialogOpen,
    dialogRef: navigationDialogRef,
    initialFocusRef: navigationPrimaryRef,
    onClose: cancelNavigation,
    closeDisabled: navigationConfirming,
  });

  const requestPreview = async () => {
    if (operationRef.current || activeUploadsRef.current.size > 0 || requestIntentRef.current) return null;
    operationRef.current = "preview";
    setOperation("preview");
    setPreviewError(null);
    try {
      const saved = await flushSave();
      const serverPreview = await onPreview(saved.revision);
      setPreview(serverPreview);
      setPreviewExpired(false);
      return serverPreview;
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : "预览生成失败");
      return null;
    } finally {
      operationRef.current = null;
      setOperation(null);
    }
  };

  const renewExpiredPreview = async () => {
    setPreviewExpired(true);
    try {
      const fresh = await onPreview(revisionRef.current);
      setPreview(fresh);
      setPreviewExpired(false);
    } catch {
      setPreviewError("预览链接已过期，请重新申请");
    }
  };

  const compareConflict = async () => {
    if (draftRecoveryState === "choice") return;
    const server = await onReload();
    setConflictServer(server);
    setConflictDialogOpen(true);
  };

  const keepLocalConflict = async () => {
    if (!conflictServer) return;
    const saved = await performSave(casesRef.current, conflictServer.revision, true);
    replaceCases(toEditorCases(saved.cases, casesRef.current));
    replaceVideo(toEditorVideo(saved.video));
    updateSaveState("saved");
    setConflictDialogOpen(false);
  };

  const chooseRecoveryCandidate = (recovery: OwnerDraftRecoveryRecord) => {
    if (!recoveryResolutionIdsRef.current.has(recovery.recordId)) return;
    recoveryRecordRef.current = recovery;
    setRecoveryCandidates([]);
    replaceCases(toEditorCases(recovery.draft.cases, casesRef.current));
    replaceVideo(toEditorVideo(recovery.draft.video));
    revisionRef.current = recovery.baseRevision;
    setRevision(recovery.baseRevision);
    if (recovery.baseRevision === initialDraft.revision) {
      setConflictLocal(null);
      setConflictServer(null);
      updateSaveState("failed");
      setDraftRecoveryState("restored");
      return;
    }
    setConflictLocal(recovery.draft);
    setConflictServer(initialDraft);
    updateSaveState("conflict");
    setDraftRecoveryState("conflict");
  };

  const useServerConflict = () => {
    if (!conflictServer) return;
    serverConfirmedDraftRef.current = conflictServer;
    revisionRef.current = conflictServer.revision;
    setRevision(conflictServer.revision);
    replaceCases(toEditorCases(conflictServer.cases, casesRef.current));
    replaceVideo(toEditorVideo(conflictServer.video));
    setConflictLocal(null);
    setConflictServer(null);
    setConflictDialogOpen(false);
    updateSaveState("saved");
    videoDraftPendingRef.current = false;
    clearOwnerVideoDraftRecovery(recoveryScope);
    clearKnownRecoveryRecords();
    recoveryRecordRef.current = null;
    setRecoveryCandidates([]);
    setDraftRecoveryState(null);
    onConflictCleared();
  };

  const discardRecoveredDraft = () => {
    serverConfirmedDraftRef.current = initialDraft;
    revisionRef.current = initialDraft.revision;
    setRevision(initialDraft.revision);
    replaceCases(toEditorCases(initialDraft.cases, casesRef.current));
    replaceVideo(toEditorVideo(initialDraft.video));
    setConflictLocal(null);
    setConflictServer(null);
    setDraftRecoveryState(null);
    updateSaveState("saved");
    videoDraftPendingRef.current = false;
    clearOwnerVideoDraftRecovery(recoveryScope);
    clearKnownRecoveryRecords();
    recoveryRecordRef.current = null;
    setRecoveryCandidates([]);
    onConflictCleared();
  };

  const selected = cases[selectedIndex];
  const requestPrimaryPreview = useCallback(async () => {
    const mediaId = casesRef.current[selectedIndex]?.primaryMediaId;
    if (!mediaId) {
      setPrimaryPreviewUrl(null);
      setPrimaryPreviewError(false);
      return;
    }
    try {
      const signed = await requestMediaUrlRef.current(mediaId);
      if (casesRef.current[selectedIndex]?.primaryMediaId === mediaId) {
        setPrimaryPreviewUrl(signed.url);
        setPrimaryPreviewError(false);
      }
    } catch {
      setPrimaryPreviewUrl(null);
      setPrimaryPreviewError(true);
    }
  }, [selectedIndex]);

  useEffect(() => {
    setPrimaryPreviewUrl(null);
    setPrimaryPreviewError(false);
    void requestPrimaryPreview();
  }, [requestPrimaryPreview, selected?.primaryMediaId]);

  const draftForValidation = useMemo(
    () => buildOwnerDraftPayload(cases, revision, completeVideo(video)),
    [cases, revision, video],
  );
  const videoProblem = incompleteVideoMessage(video);
  const publishIssues = useMemo<EditorPublishIssue[]>(() => [
    ...getOwnerPublishIssues(draftForValidation),
    ...(videoProblem ? [{ message: videoProblem, field: "video" as const }] : []),
  ], [draftForValidation, videoProblem]);
  const blockers = publishIssues.map((issue) => issue.message);

  const locatePublishIssue = (issue: EditorPublishIssue) => {
    if (issue.caseIndex !== undefined) setSelectedIndex(issue.caseIndex);
    window.setTimeout(() => {
      const target = document.querySelector<HTMLElement>(`[data-owner-field="${issue.field}"]`);
      target?.scrollIntoView?.({ block: "center", behavior: "smooth" });
      const focusTarget = target?.matches("input, textarea, button")
        ? target
        : target?.querySelector<HTMLElement>("input, textarea, button, [tabindex]");
      focusTarget?.focus();
    }, 0);
  };

  const updateSelected = (patch: Partial<OwnerCaseDraft>) => {
    if (!selected) return;
    scheduleSave(cases.map((item, index) => index === selectedIndex ? { ...item, ...patch } : item));
  };

  const addCase = () => {
    const next = [...cases, blankCase()];
    setSelectedIndex(next.length - 1);
    scheduleSave(next);
  };

  const mergeUploadedMedia = (
    targetCaseId: string,
    uploaded: UploadedContentMedia,
  ) => {
    if (uploaded.mediaType !== "image") return;
    const current = casesRef.current;
    if (!current.some((item) => item.editorCaseId === targetCaseId)) return;
    scheduleSave(current.map((item) => item.editorCaseId === targetCaseId
      ? { ...replaceOwnerCaseMedia(item, "primary", uploaded.mediaId), editorCaseId: item.editorCaseId }
      : item));
  };

  const mergeUploadedImagePair = (targetCaseId: string, pair: OwnerImageUploadPair) => {
    if (
      pair.thumbnail.derivativeRole !== "thumbnail"
      || pair.display.derivativeRole !== "display"
      || pair.thumbnail.mediaId === pair.display.mediaId
    ) return;
    const current = casesRef.current;
    if (!current.some((item) => item.editorCaseId === targetCaseId)) return;
    scheduleSave(current.map((item) => item.editorCaseId === targetCaseId
      ? {
          ...item,
          detailImages: [...item.detailImages, {
            alt: `${item.title.trim() || "房源"} 详情图 ${item.detailImages.length + 1}`,
            thumbnailMediaId: pair.thumbnail.mediaId,
            displayMediaId: pair.display.mediaId,
          }],
          privacyConfirmed: false,
        }
      : item));
  };

  const mergeUploadedVideo = (uploaded: UploadedContentMedia) => {
    if (uploaded.mediaType !== "video") return;
    const current = videoRef.current;
    const next = {
      ...current,
      mediaId: uploaded.mediaId,
      posterMediaId: completeVideo(current) ? null : current.posterMediaId,
    };
    replaceVideo(next);
    videoDraftPendingRef.current = true;
    if (!writeOwnerVideoDraftRecovery(recoveryScope, revisionRef.current, next)) {
      setRecoveryWarning("视频更换无法写入本地恢复记录，请保持页面打开并尽快补全。");
    }
    if (completeVideo(next)) {
      scheduleSave(casesRef.current, next);
      setVideoStatus("视频与封面已关联，正在保存草稿");
    } else {
      localChangeEpochRef.current += 1;
      armHistoryGuard();
      updateSaveState("failed");
      setVideoStatus("新视频已上传，还需要上传新封面后才能保存和发布");
    }
  };

  const mergeUploadedVideoPoster = (uploaded: UploadedContentMedia) => {
    if (uploaded.mediaType !== "image") return;
    const next = { ...videoRef.current, posterMediaId: uploaded.mediaId };
    replaceVideo(next);
    videoDraftPendingRef.current = true;
    if (!writeOwnerVideoDraftRecovery(recoveryScope, revisionRef.current, next)) {
      setRecoveryWarning("视频更换无法写入本地恢复记录，请保持页面打开并尽快补全。");
    }
    if (completeVideo(next)) {
      scheduleSave(casesRef.current, next);
      setVideoStatus("视频与封面已关联，正在保存草稿");
    } else {
      localChangeEpochRef.current += 1;
      armHistoryGuard();
      updateSaveState("failed");
      setVideoStatus("视频封面已上传，还需要上传展示视频后才能保存和发布");
    }
  };

  const removeDraftVideo = () => {
    const next = toEditorVideo(null);
    videoDraftPendingRef.current = false;
    clearOwnerVideoDraftRecovery(recoveryScope);
    scheduleSave(casesRef.current, next);
    setVideoStatus("工作过程视频关联已移除，正在保存草稿");
  };

  const cancelIncompleteVideo = () => {
    const restored = toEditorVideo(initialDraft.video);
    videoDraftPendingRef.current = false;
    clearOwnerVideoDraftRecovery(recoveryScope);
    replaceVideo(restored);
    setVideoStatus("已放弃未完成的视频更换");
    if (saveStateRef.current === "failed" && !timerRef.current && !savePromiseRef.current) {
      const localAfterVideoDiscard = buildOwnerDraftPayload(
        casesRef.current,
        revisionRef.current,
        completeVideo(restored),
      );
      if (sameDraft(localAfterVideoDiscard, serverConfirmedDraftRef.current)) {
        updateSaveState("saved");
      }
    }
  };

  const removeDetail = (index: number) => {
    if (!selected) return;
    updateSelected({
      detailImages: selected.detailImages.filter((_item, itemIndex) => itemIndex !== index),
      privacyConfirmed: false,
    });
  };

  const sameIntent = (left: OwnerActionIntent | null, right: OwnerActionIntent) => left?.operation === right.operation
    && left.idempotencyKey === right.idempotencyKey
    && (left.operation === "publish"
      ? right.operation === "publish" && left.revision === right.revision
      : right.operation === "rollback" && left.version === right.version);

  const submitIntent = async (intent: OwnerActionIntent) => {
    requestIntentRef.current = intent;
    const accepted = intent.operation === "publish"
      ? await onPublish({ revision: intent.revision, idempotencyKey: intent.idempotencyKey })
      : await onRollback({ version: intent.version, idempotencyKey: intent.idempotencyKey });
    if (sameIntent(requestIntentRef.current, intent)) requestIntentRef.current = null;
    setRequestRecovery((current) => sameIntent(current?.intent || null, intent) ? null : current);
    setLocalJob(acceptedJob(accepted, intent.operation));
    setPublishOpen(true);
    onJobStarted(accepted.jobId);
  };

  const isTerminalJob = (candidate: PublishJob | null | undefined) => !!candidate && ["succeeded", "failed", "rolled_back"].includes(candidate.status);
  const releaseOperationBlockedNow = () => {
    const currentJob = localJob || publishJob;
    return !!operationRef.current
      || activeUploadsRef.current.size > 0
      || saveStateRef.current === "saving"
      || (!!currentJob && !isTerminalJob(currentJob))
      || (!!activeJobId && (!publishJob || !isTerminalJob(publishJob)));
  };
  const runReleaseExclusive = async <T,>(kind: "publish" | "rollback", action: () => Promise<T>): Promise<T | undefined> => {
    if (releaseOperationBlockedNow()) return undefined;
    operationRef.current = kind;
    setOperation(kind);
    try {
      return await action();
    } finally {
      if (operationRef.current === kind) {
        operationRef.current = null;
        setOperation(null);
      }
    }
  };

  const performPublishAttempt = async () => {
    setRequestRecovery(null);
    let intent: Extract<OwnerActionIntent, { operation: "publish" }> | null = null;
    try {
      const saved = await flushSave();
      intent = { operation: "publish", revision: saved.revision, idempotencyKey: idempotencyKey("publish-owner") };
      await submitIntent(intent);
    } catch (error) {
      if (intent) {
        setRequestRecovery({
          message: error instanceof Error ? error.message : "发布请求失败，请重试",
          intent,
        });
      }
    }
  };
  const launchPublish = () => runReleaseExclusive("publish", performPublishAttempt);

  const rollback = async (version: string) => {
    if (operationRef.current !== "history" || activeUploadsRef.current.size > 0 || saveStateRef.current === "saving") {
      throw new Error("当前有其他操作正在进行，请完成后再恢复历史版本。");
    }
    operationRef.current = "rollback";
    setOperation("rollback");
    const current = requestIntentRef.current;
    const intent: Extract<OwnerActionIntent, { operation: "rollback" }> = current?.operation === "rollback"
      && current.version === version
      ? current
      : { operation: "rollback", version, idempotencyKey: idempotencyKey("rollback-owner") };
    requestIntentRef.current = intent;
    try {
      await submitIntent(intent);
    } catch (error) {
      operationRef.current = "history";
      setOperation("history");
      throw error;
    } finally {
      if (operationRef.current === "rollback") {
        operationRef.current = null;
        setOperation(null);
      }
    }
  };

  const job = localJob || publishJob;
  const jobInProgress = !!activeJobId || !!job && !["succeeded", "failed", "rolled_back"].includes(job.status);
  const retryFailedJob = job?.status === "failed" && getPublishFailurePresentation(job.errorCode).canRetry
    ? () => {
        const kind = job.operation === "rollback" ? "rollback" : "publish";
        void runReleaseExclusive(kind, async () => {
          requestIntentRef.current = null;
          if (job.operation === "rollback" && job.targetVersion) {
            const intent: Extract<OwnerActionIntent, { operation: "rollback" }> = {
              operation: "rollback",
              version: job.targetVersion,
              idempotencyKey: idempotencyKey("rollback-owner"),
            };
            try {
              await submitIntent(intent);
            } catch (error) {
              setRequestRecovery({
                message: error instanceof Error ? error.message : "回滚请求失败，请重试",
                intent,
              });
            }
          } else {
            await performPublishAttempt();
          }
        });
      }
    : undefined;
  const retryRequestRecovery = () => {
    const recovery = requestRecovery;
    if (!recovery) return;
    void runReleaseExclusive(recovery.intent.operation, async () => {
      try {
        await submitIntent(recovery.intent);
      } catch (error) {
        setRequestRecovery({
          message: error instanceof Error
            ? error.message
            : recovery.intent.operation === "rollback" ? "回滚请求失败，请重试" : "发布请求失败，请重试",
          intent: recovery.intent,
        });
      }
    });
  };
  const visibleCount = cases.filter((item) => item.isVisible).length;
  const missingCases = Math.max(0, 4 - visibleCount);
  const conflictDiff = conflictServer
    ? diffOwnerDraft(
        conflictServer,
        buildOwnerDraftPayload(
          cases,
          conflictServer.revision,
          completeVideo(video),
        ),
      )
    : [];
  const conflictFieldLabel = (field: string) => ({
    "案例": "房源",
    "标题": "房源展示名称",
    "说明": "我们为这套房做了什么",
    "主图": "首图",
    "详情图": "房源照片",
    "工作过程视频": "展示视频",
  }[field] || field);
  const conflictSubject = (item: { caseId: string; field: string }) => {
    const field = conflictFieldLabel(item.field);
    if (item.caseId === "owner-video") return `展示视频 · ${field}`;
    if (isAdmin) return `${item.caseId} · ${field}`;
    const localIndex = conflictLocal?.cases.findIndex((ownerCase) => ownerCase.caseId === item.caseId) ?? -1;
    const serverIndex = conflictServer?.cases.findIndex((ownerCase) => ownerCase.caseId === item.caseId) ?? -1;
    const parsedNewIndex = item.caseId.startsWith("new-") ? Number(item.caseId.slice(4)) : -1;
    const index = localIndex >= 0 ? localIndex : serverIndex >= 0 ? serverIndex : parsedNewIndex;
    const ownerCase = (localIndex >= 0 ? conflictLocal?.cases[localIndex] : null)
      || (serverIndex >= 0 ? conflictServer?.cases[serverIndex] : null);
    const title = ownerCase?.title.trim();
    return `${index >= 0 ? `第 ${index + 1} 套房源` : "房源"}${title ? `“${title}”` : ""} · ${field}`;
  };
  const hasUnsavedChanges = saveState !== "saved" || activeUploads.size > 0;
  const operationBlocked = saveState === "saving" || activeUploads.size > 0 || operation !== null || jobInProgress;
  const interactiveOperationBlocked = activeUploads.size > 0 || operation !== null || jobInProgress;
  const readyForPublish = blockers.length === 0 && activeUploads.size === 0;
  const statusLabel = saveState === "conflict"
    ? "需要处理内容冲突"
    : saveState === "failed"
      ? "修改尚未保存"
      : saveState === "saving"
        ? "正在保存修改"
        : readyForPublish
          ? "内容已保存，可以发布"
          : "内容还需完善";
  const statusExplanation = saveState === "conflict"
    ? "服务器内容已有更新，本地修改仍然保留。请先比较两个版本。"
    : videoProblem
      ? "展示视频和封面必须同时准备好，当前更换尚未完成。"
    : saveState === "failed"
      ? "本地修改仍在当前页面，请再次保存。"
      : readyForPublish
        ? "四套可见房源和所需图片均已准备好。"
        : `发布前还有 ${publishIssues.length} 项内容需要处理，下面已逐项列出。`;
  const nextStep = hasUnsavedChanges
    ? videoProblem
      ? "补全展示视频和封面"
      : "保存修改"
    : readyForPublish && isAdmin
      ? "发布到小程序"
      : readyForPublish
        ? "查看手机效果并联系管理员发布"
        : "按发布前检查逐项处理";
  const actions: ContentEditorAction[] = [
    ...(saveState === "conflict" && draftRecoveryState !== "choice" ? [{
      key: "compare",
      label: "查看服务器与本地差异",
      kind: "secondary" as const,
      onClick: compareConflict,
    }] : []),
    ...(hasUnsavedChanges ? [{
      key: "save",
      label: "保存修改",
      kind: "primary" as const,
      disabled: saveState === "conflict" || activeUploads.size > 0 || !!videoProblem,
      onClick: performSave,
    }] : []),
    {
      key: "preview",
      label: "查看手机效果",
      kind: !hasUnsavedChanges && !isAdmin ? "primary" : "secondary",
      disabled: !!videoProblem || interactiveOperationBlocked,
      onClick: requestPreview,
    },
    {
      key: "history",
      label: "历史版本",
      kind: "secondary",
      adminOnly: true,
      disabled: operationBlocked,
      onClick: () => {
        if (operationRef.current || activeUploadsRef.current.size > 0 || saveStateRef.current === "saving" || jobInProgress) return;
        operationRef.current = "history";
        setOperation("history");
        setPublishOpen(false);
        setHistoryOpen(true);
      },
    },
    ...(!hasUnsavedChanges && isAdmin ? [{
      key: "publish",
      label: "发布到小程序",
      kind: "primary" as const,
      disabled: !readyForPublish || operationBlocked,
      onClick: launchPublish,
    }] : []),
  ];
  const previewManifest = preview && "schema" in preview ? preview : null;

  return (
    <>
      <ContentEditorShell
        title="业主托管招商"
        explanation="维护业主在小程序中看到的真实房源、照片和服务过程。"
        statusLabel={statusLabel}
        statusExplanation={statusExplanation}
        completion={`${visibleCount}/4 套可见房源`}
        nextStep={nextStep}
        role={isAdmin ? "admin" : "operator"}
        saveState={saveState}
        hasUnsavedChanges={false}
        readyForPublish={readyForPublish}
        onSave={flushSave}
        onNavigate={onNavigate}
        actions={actions}
        form={(
          <div className={styles.ownerEditorForm}>

      {weakNetwork ? (
        <section className={styles.networkState} role="alert">
          <div><strong>网络连接较弱</strong><span>本地内容仍在当前页面，恢复连接后可继续保存。</span></div>
          <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`} onClick={() => void performSave().catch(() => undefined)}>网络恢复后重试保存</button>
        </section>
      ) : null}

      {recoveryWarning ? (
        <section className={styles.persistentError} role="alert">
          <strong>本地恢复保护受限</strong>
          <span>{recoveryWarning}</span>
        </section>
      ) : null}

      {draftRecoveryState === "choice" ? (
        <section className={styles.persistentError} role="alert">
          <strong>检测到 {recoveryCandidates.length} 份未解决的本地副本</strong>
          <span>这些副本可能来自不同标签页，无法安全判断先后。请选择要恢复的一份，或明确放弃全部副本并使用服务器版本。</span>
          <ul className={styles.diffList}>
            {recoveryCandidates.map((record) => {
              const title = record.draft.cases[0]?.title || "未命名房源";
              return (
                <li key={record.recordId}>
                  <span><strong>{title}</strong> · {record.draft.cases.length} 套房源 · {new Date(record.writtenAt).toLocaleString("zh-CN")}</span>
                  <button
                    type="button"
                    className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`}
                    aria-label={`恢复副本：${title}`}
                    onClick={() => chooseRecoveryCandidate(record)}
                  >选择此副本</button>
                </li>
              );
            })}
          </ul>
          <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`} onClick={discardRecoveredDraft}>放弃全部恢复副本，使用服务器版本</button>
        </section>
      ) : null}

      {draftRecoveryState === "restored" ? (
        <section className={styles.persistentError} role="alert">
          <strong>已恢复未保存的本地副本</strong>
          <span>这份本地修改尚未写回服务器。</span>
          <div className={styles.modalActions}>
            <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`} onClick={discardRecoveredDraft}>放弃恢复副本</button>
            <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} disabled={!!videoProblem} onClick={() => void performSave().catch(() => undefined)}>{videoProblem ? "先补全视频" : "保存恢复副本"}</button>
          </div>
        </section>
      ) : null}

      {draftRecoveryState === "conflict" ? (
        <section className={styles.persistentError} role="alert">
          <strong>恢复副本与服务器版本冲突</strong>
          <span>服务器内容已有更新。自动保存已暂停，请比较后明确选择。</span>
        </section>
      ) : null}

      {requestRecovery ? (
        <section className={styles.persistentError} role="alert">
          <strong>{requestRecovery.intent.operation === "rollback" ? "回滚请求未提交" : "发布请求未提交"}</strong>
          <span>{requestRecovery.message}。草稿仍然保留。</span>
          <button type="button" disabled={operation !== null || saveState === "saving" || activeUploads.size > 0 || jobInProgress} aria-busy={operation !== null} className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} onClick={retryRequestRecovery}>
            {requestRecovery.intent.operation === "rollback" ? `重试回滚 ${requestRecovery.intent.version}` : "重试发布请求"}
          </button>
        </section>
      ) : null}

      {publishIssues.length > 0 ? (
        <section className={styles.publishIssues} role="alert" aria-label="发布前检查">
          <strong>发布前还需处理 {publishIssues.length} 项</strong>
          <ul>
            {publishIssues.map((issue, index) => (
              <li key={`${issue.field}-${issue.caseIndex ?? "all"}-${index}`}>
                <button
                  type="button"
                  className={styles.publishIssueButton}
                  aria-label={`处理：${issue.message}`}
                  onClick={() => locatePublishIssue(issue)}
                >{issue.message}</button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <div className={styles.ownerEditorLayout} data-testid="owner-editor-workspace">
        <aside className={styles.ownerDirectory} aria-label="真实房源展示目录" data-owner-field="directory" tabIndex={-1}>
          <div className={styles.paneHeader}><h2>真实房源展示</h2><strong>{cases.length} 套房源</strong><p>选择一套房源填写内容，也可以调整展示顺序。</p></div>
          {cases.length === 0 ? (
            <div className={styles.dedicatedState}>
              <h2>还没有真实房源展示</h2>
              <p>从一个真实房源开始，先记录具体工作事实。</p>
              <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} onClick={addCase}>新增第一套房源</button>
            </div>
          ) : (
            <ol className={styles.caseList}>
              {cases.map((item, index) => (
                <li key={item.editorCaseId} data-selected={selectedIndex === index ? "true" : "false"}>
                  <button type="button" className={styles.caseSelect} onClick={() => setSelectedIndex(index)}>
                    <span className={styles.caseNumber}>{String(index + 1).padStart(2, "0")}</span>
                    <span><strong>{item.title || "未命名房源"}</strong><small>{item.isVisible ? "显示" : "隐藏"} · {item.primaryMediaId ? "有首图" : "缺首图"}</small></span>
                  </button>
                  <div className={styles.sortActions}>
                    <button type="button" style={{ minWidth: 44, width: 44, minHeight: 44 }} className={`touchTarget ${styles.touchTarget} ${styles.iconButton}`} disabled={index === 0} aria-label={`上移${item.title}`} onClick={() => { const next = moveOwnerCase(cases, index, index - 1); setSelectedIndex(index - 1); scheduleSave(next); }}><ArrowUpOutlined /></button>
                    <button type="button" style={{ minWidth: 44, width: 44, minHeight: 44 }} className={`touchTarget ${styles.touchTarget} ${styles.iconButton}`} disabled={index === cases.length - 1} aria-label={`下移${item.title}`} onClick={() => { const next = moveOwnerCase(cases, index, index + 1); setSelectedIndex(index + 1); scheduleSave(next); }}><ArrowDownOutlined /></button>
                  </div>
                </li>
              ))}
            </ol>
          )}
          {cases.length ? <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.textButton}`} onClick={addCase}><PlusOutlined aria-hidden="true" /> 新增房源展示</button> : null}
        </aside>

        <section className={styles.ownerFields} aria-label="房源内容编辑区">
          {selected ? (
            <>
              <div className={styles.paneHeader}><h2>第 {selectedIndex + 1} 套房源</h2><strong>填写展示内容</strong></div>
              <label className={styles.field}><span>房源展示名称</span><input data-owner-field="title" value={selected.title} maxLength={80} onChange={(event) => updateSelected({ title: event.target.value })} aria-label="房源展示名称" /><small>例如：灵山湾海景两居</small><small>使用模糊位置或房型，不公开门牌；最多 80 字</small></label>
              <label className={styles.field}><span>我们为这套房做了什么</span><textarea data-owner-field="description" value={selected.description || ""} maxLength={240} onChange={(event) => updateSelected({ description: event.target.value || null })} aria-label="我们为这套房做了什么" /><small>例如：重新整理客厅动线，并完成入住前检查</small><small>写具体动作与事实，不填写未经证实的收益</small></label>
              <div className={styles.visibilityRow}>
                <label><input type="checkbox" checked={selected.isVisible} onChange={(event) => updateSelected({ isVisible: event.target.checked })} /> 在小程序中显示</label>
                <label><input data-owner-field="privacy" type="checkbox" checked={selected.privacyConfirmed} onChange={(event) => updateSelected({ privacyConfirmed: event.target.checked })} /> 已确认图片授权且无门牌、人脸、联系方式等隐私</label>
              </div>
              <section className={styles.mediaSection} aria-labelledby="primary-media-title" data-owner-field="primary" tabIndex={-1}>
                <div><h2 id="primary-media-title">首图</h2><p>选择一张最能看清房源整体情况的照片。</p></div>
                {selected.primaryMediaId ? (
                  <div className={styles.mediaReference}>
                    {primaryPreviewUrl ? (
                      // eslint-disable-next-line @next/next/no-img-element -- Task 5 signed URLs stay ephemeral in component state
                      <img src={primaryPreviewUrl} alt={`${selected.title} 当前主图`} onError={() => void requestPrimaryPreview()} />
                    ) : <span aria-hidden="true">{primaryPreviewError ? "!" : "…"}</span>}
                    <div>
                      <strong>{primaryPreviewError ? "首图预览已过期" : "首图已上传"}</strong>
                      {primaryPreviewError ? <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.inlineAction}`} onClick={() => void requestPrimaryPreview()}>重新申请预览</button> : null}
                    </div>
                  </div>
                ) : (
                  <div className={styles.mediaEmpty}><strong>还没有首图</strong><span>先上传一张照片；需要展示的房源必须有首图。</span></div>
                )}
                <MediaUploader
                  kind="image"
                  buttonLabel="上传房源首图"
                  onUpload={onUploadImage}
                  onUploaded={(uploaded) => mergeUploadedMedia(selected.editorCaseId, uploaded)}
                  onActiveChange={(active) => setUploadActive(`primary:${selected.editorCaseId}`, active)}
                />
                <div data-owner-field="details" tabIndex={-1}><h2>房源照片</h2><p>例如：客厅、卧室、窗外景观和整理后的细节。</p></div>
                {selected.detailImages.length > 0 ? (
                  <ul className={styles.detailMediaList} aria-label="房源照片列表">
                    {selected.detailImages.map((image, index) => (
                      <li key={`${image.thumbnailMediaId}:${image.displayMediaId}`}>
                        <span>{image.alt}</span>
                        <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`} onClick={() => removeDetail(index)} aria-label={`移除详情图 ${image.alt}`}>移除</button>
                      </li>
                    ))}
                  </ul>
                ) : <div className={styles.mediaEmpty}><strong>还没有其他房源照片</strong><span>可选；上传后会按当前顺序展示。</span></div>}
                <div className={styles.detailMediaList}>
                  <MediaUploader<OwnerImageUploadPair>
                    kind="image"
                    buttonLabel="上传房源照片"
                    onUpload={onUploadImagePair}
                    onUploaded={(pair) => mergeUploadedImagePair(selected.editorCaseId, pair)}
                    onActiveChange={(active) => setUploadActive(`detail-source:${selected.editorCaseId}`, active)}
                    disabled={selected.detailImages.length >= 20}
                  />
                  <p className={styles.scopeNote}>直接选择原图即可，系统会自动处理成适合小程序展示的尺寸。</p>
                </div>
              </section>
              <section className={styles.mediaSection} aria-labelledby="video-media-title" data-owner-field="video" tabIndex={-1}>
                <div><h2 id="video-media-title">展示视频（可选）</h2><p>例如：整理、清洁、拍摄或入住前检查的真实过程。</p></div>
                <p className={styles.scopeNote}>视频需要配一张封面。发布后默认静音，由用户点击播放。</p>
                {completeVideo(video) ? (
                  <div className={styles.mediaReference}>
                    <div>
                      <strong>展示视频和封面已上传</strong>
                    </div>
                    <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`} onClick={removeDraftVideo}>移除工作过程视频</button>
                  </div>
                ) : videoProblem ? (
                  <div className={styles.persistentError} role="status">
                    <strong>视频更换尚未完成</strong>
                    <span>{videoStatus?.startsWith("已恢复") ? videoStatus : "请补齐缺少的视频或封面，也可以放弃这次更换。"}</span>
                    <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`} onClick={cancelIncompleteVideo}>放弃未完成的视频更换</button>
                  </div>
                ) : null}
                <MediaUploader
                  kind="video"
                  onUpload={onUploadVideo}
                  onUploaded={mergeUploadedVideo}
                  onActiveChange={(active) => setUploadActive("process-video", active)}
                />
                <MediaUploader
                  kind="image"
                  buttonLabel="上传工作过程视频封面"
                  onUpload={onUploadImage}
                  onUploaded={mergeUploadedVideoPoster}
                  onActiveChange={(active) => setUploadActive("process-video-poster", active)}
                />
                {videoStatus && !videoProblem ? <p className={styles.scopeNote} role="status">{videoStatus}</p> : null}
              </section>
            </>
          ) : (
            <div className={styles.editorBlank}><EyeOutlined aria-hidden="true" /><p>从左侧新增或选择一套房源后开始编辑。</p></div>
          )}
        </section>

      </div>
          </div>
        )}
        renderPreview={(width: ContentPreviewWidth) => (
          <div className={styles.ownerPreviewContent}>
            {previewExpired ? (
              <div className={styles.dedicatedState} role="alert"><h3>预览链接已过期</h3><p>预览链接只在当前页面短暂使用，不会保存到正式内容。</p><button type="button" className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} onClick={() => void renewExpiredPreview()}>重新生成预览</button></div>
            ) : <MobileOwnerPreview manifest={preview} width={width} onExpired={() => void renewExpiredPreview()} />}
            {previewError ? <div className={styles.persistentError} role="alert"><strong>预览生成失败</strong><span>{previewError}</span><button type="button" className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} onClick={() => void requestPreview()}>重试预览</button></div> : null}
            {missingCases > 0 ? <div className={styles.dedicatedState}><h3>还需 {missingCases} 套可见房源才能发布</h3><p>保存和预览不受影响。补足四套真实房源后再发布。</p><button type="button" className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`} onClick={addCase}>新增房源展示</button></div> : null}
          </div>
        )}
        technicalDetails={(
          <div className={styles.technicalDetails}>
            <p>草稿内容版本：{revision}</p>
            <p>线上发布版本：{publishedVersion || "尚未发布"}</p>
            {selected?.caseId ? <p>房源内部编号：{selected.caseId}</p> : null}
            {selected?.primaryMediaId ? <p>首图媒体编号：{selected.primaryMediaId}</p> : null}
            {selected?.detailImages.map((image, index) => (
              <p key={`${image.thumbnailMediaId}:${image.displayMediaId}`}>房源照片 {index + 1}：{image.thumbnailMediaId} / {image.displayMediaId}</p>
            ))}
            {video.mediaId ? <p>视频媒体编号：{video.mediaId}</p> : null}
            {video.posterMediaId ? <p>视频封面媒体编号：{video.posterMediaId}</p> : null}
            {job ? <>
              <p>发布任务编号：{job.jobId}</p>
              <p>发布任务阶段：{job.progressStage || "未提供"}</p>
            </> : null}
            {previewManifest ? <>
              <p>首页图片：{previewManifest.hero.width} × {previewManifest.hero.height}，校验值 {previewManifest.hero.sha256}</p>
              {previewManifest.cases.flatMap((item) => [item.hero, ...item.images.flatMap((image) => [image.thumbnail, image.display])]).map((image, index) => (
                <p key={`${image.sha256}:${index}`}>预览图片 {index + 1}：{image.width} × {image.height}，校验值 {image.sha256}</p>
              ))}
              {previewManifest.video ? <p>预览视频：{previewManifest.video.width} × {previewManifest.video.height}，校验值 {previewManifest.video.sha256}</p> : null}
            </> : <p>生成手机预览后，这里会显示图片尺寸和校验值。</p>}
          </div>
        )}
      />

      {conflictDialogOpen && conflictServer ? (
        <div className={styles.modal} role="dialog" aria-modal="true" aria-label="解决内容冲突">
          <h2>解决内容冲突</h2>
          <p>服务器内容已有更新。本地副本仍保留，自动保存已暂停。</p>
          <div className={styles.diffColumns}><strong>服务器版本</strong><strong>本地副本</strong></div>
          {conflictDiff.length ? <ul className={styles.diffList}>{conflictDiff.map((item, index) => <li key={`${item.caseId}-${item.field}-${index}`}><span>{conflictSubject(item)}</span><span>{item.server}</span><span>{item.local}</span></li>)}</ul> : <p>服务器与当前本地副本没有字段差异。</p>}
          <div className={styles.modalActions}>
            <button ref={conflictPrimaryRef} type="button" className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`} onClick={useServerConflict}>使用服务器版本</button>
            <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} onClick={() => void keepLocalConflict()}>以本地副本重新保存</button>
          </div>
        </div>
      ) : null}

      {pendingNavigation && !conflictDialogOpen ? (
        <div ref={navigationDialogRef} className={styles.modal} role="dialog" aria-modal="true" aria-label="离开业主托管内容编辑" tabIndex={-1}>
          <h2>离开业主托管内容编辑</h2>
          {activeUploads.size > 0 ? (
            <p>上传仍在进行。请继续编辑，并使用上传区的“取消上传”操作后再离开。</p>
          ) : videoProblem ? (
            <p>{video.mediaId ? "新视频还缺少封面，离开后这次更换不会提交到服务器。" : "视频封面还缺少展示视频，离开后这次更换不会提交到服务器。"}</p>
          ) : (
            <p>当前改动尚未完成保存。保存成功后再离开，可避免丢失本地内容。</p>
          )}
          <div className={styles.modalActions}>
            <button ref={navigationPrimaryRef} type="button" className={`touchTarget ${styles.touchTarget} ${styles.secondaryButton}`} disabled={navigationConfirming} onClick={cancelNavigation}>继续编辑</button>
            {activeUploads.size === 0 && !videoProblem ? (
              <button type="button" className={`touchTarget ${styles.touchTarget} ${styles.primaryButton}`} disabled={navigationConfirming} aria-busy={navigationConfirming} onClick={() => void confirmPendingNavigation()}>
                {navigationConfirming ? "正在保存…" : "保存后离开"}
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      <PublishDialog
        open={publishOpen}
        job={job}
        isAdmin={isAdmin}
        onClose={() => setPublishOpen(false)}
        onRetry={retryFailedJob}
        retryDisabled={operation !== null || saveState === "saving" || activeUploads.size > 0 || jobInProgress}
        trackingStatus={publishTrackingStatus}
        trackingError={publishTrackingError}
        onRetryTracking={onRetryPublishTracking}
        contentTitle="业主托管招商"
      />
      <ReleaseHistoryDrawer
        open={historyOpen}
        releases={releases}
        canRollback={isAdmin}
        onClose={() => {
          setHistoryOpen(false);
          if (operationRef.current === "history") {
            operationRef.current = null;
            setOperation(null);
          }
        }}
        actionsDisabled={operation !== "history" || jobInProgress || saveState === "saving" || activeUploads.size > 0}
        onRollback={rollback}
        status={releasesStatus}
        onRetry={onRetryReleases}
      />
    </>
  );
}
