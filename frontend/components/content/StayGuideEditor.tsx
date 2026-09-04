"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ContentClientError,
  ContentRelease,
  PublishJob,
  PublishJobAccepted,
  RollbackStructuredReleaseInput,
  SaveStructuredDraftInput,
  StayGuideDraft,
  StructuredDraftRevision,
  StructuredGuidePreview,
} from "@/features/content/types";
import {
  isStructuredContentTerminalJobStatus,
  type StructuredDraftConflict,
} from "@/hooks/content/useStructuredContent";
import { useModalDialog } from "@/hooks/useModalDialog";
import {
  ContentEditorShell,
  type ContentEditorAction,
  type ContentEditorRole,
  type ContentPreviewWidth,
} from "./ContentEditorShell";
import { MobileStayGuidePreview } from "./MobileStayGuidePreview";
import { PublishDialog } from "./PublishDialog";
import { ReleaseHistoryDrawer } from "./ReleaseHistoryDrawer";
import styles from "./content-center.module.css";

const STANDARD_SECTIONS = [
  ["arrivalDeparture", "入住与退房时间", "几点可以入住和退房？"],
  ["parking", "停车说明", "到达后怎么停车？"],
  ["wifiAndDevices", "Wi-Fi 与常用设备", "网络和常用设备怎么使用？"],
  ["houseRules", "住宿规则", "住宿期间需要注意什么？"],
  ["checkOut", "退房说明", "退房前需要做什么？"],
  ["support", "联系管家", "遇到问题联系谁？"],
] as const;

const SECTION_NAMES: Record<string, string> = Object.fromEntries([
  ...STANDARD_SECTIONS.map(([key, label]) => [key, label]),
  ["faq", "常见问题"],
]);

interface EditorIssue {
  key: string;
  target: string;
  message: string;
}

const CORE_VISIBILITY_MESSAGE = "至少显示一项核心入住说明；常见问题不能单独发布。";

function fieldId(target: string): string {
  return `stay-guide-${target.replace(/\./g, "-")}`;
}

function errorId(issue: EditorIssue): string {
  return `stay-guide-error-${issue.key.replace(/\./g, "-")}`;
}

export interface StayGuideEditorProps {
  initialDraft: StructuredDraftRevision<"stay_guide">;
  role: ContentEditorRole;
  publishedVersion: string | null;
  conflict: StructuredDraftConflict<"stay_guide"> | null;
  activeJobId: string | null;
  publishJob: PublishJob | null;
  releases: ContentRelease[];
  releasesStatus: "loading" | "success" | "error";
  publishTrackingStatus: "idle" | "loading" | "success" | "error";
  publishTrackingError?: string | null;
  onSave(input: SaveStructuredDraftInput<"stay_guide">): Promise<StructuredDraftRevision<"stay_guide">>;
  onRefresh(): Promise<StructuredDraftRevision<"stay_guide">>;
  onClearConflict(): void;
  onPreview(revision: number): Promise<StructuredGuidePreview<"stay_guide">>;
  onPublish(input: { revision: number; idempotencyKey: string }): Promise<PublishJobAccepted>;
  onJobStarted(job: PublishJobAccepted): void;
  onRollback(input: RollbackStructuredReleaseInput): Promise<PublishJobAccepted>;
  onRetryReleases?(): void;
  onRetryPublishTracking?(): void;
  onNavigate?(href: string): void;
}

function normalizeText(value: string): string {
  return value.replace(/\r\n?/g, "\n").trim();
}

function containsHtml(value: string): boolean {
  return /<\/?[a-z][^>]*>/i.test(value);
}

function cleanOptional(value: string | undefined): string | undefined {
  const cleaned = normalizeText(value || "");
  return cleaned || undefined;
}

function sanitizeDraft(draft: StayGuideDraft): StayGuideDraft {
  const cleanSection = <T extends { summary: string; details?: string; visible: boolean; image?: { mediaId: string } }>(section: T): T => ({
    ...section,
    summary: normalizeText(section.summary),
    details: cleanOptional(section.details),
  });
  return {
    title: normalizeText(draft.title),
    intro: normalizeText(draft.intro),
    sections: {
      arrivalDeparture: cleanSection(draft.sections.arrivalDeparture),
      parking: cleanSection(draft.sections.parking),
      wifiAndDevices: cleanSection(draft.sections.wifiAndDevices),
      houseRules: cleanSection(draft.sections.houseRules),
      checkOut: cleanSection(draft.sections.checkOut),
      support: cleanSection(draft.sections.support),
      faq: {
        ...cleanSection(draft.sections.faq),
        items: draft.sections.faq.items.map((item) => ({
          question: normalizeText(item.question),
          answer: normalizeText(item.answer),
        })),
      },
    },
  };
}

function checkText(issues: EditorIssue[], target: string, label: string, value: string, maximum: number, required = true) {
  const cleaned = normalizeText(value);
  if (required && !cleaned) issues.push({ key: `${target}.required`, target, message: `${label}：请填写${label === "页面标题" ? "公开页面标题" : "内容"}。` });
  else if (cleaned.length > maximum) issues.push({ key: `${target}.maximum`, target, message: `${label}：最多 ${maximum} 字，请删减后再保存。` });
  if (containsHtml(value)) issues.push({ key: `${target}.html`, target, message: `${label}：不能包含 HTML 标签，请改为普通文字。` });
}

function validateDraft(draft: StayGuideDraft): EditorIssue[] {
  const issues: EditorIssue[] = [];
  checkText(issues, "title", "页面标题", draft.title, 80);
  checkText(issues, "intro", "简短介绍", draft.intro, 240);
  for (const [key, label] of STANDARD_SECTIONS) {
    const section = draft.sections[key];
    checkText(issues, `${key}.summary`, `${label} · 简短文字`, section.summary, 240);
    if (section.details) checkText(issues, `${key}.details`, `${label} · 补充说明`, section.details, 2000, false);
  }
  const faq = draft.sections.faq;
  checkText(issues, "faq.summary", "常见问题 · 简短文字", faq.summary, 240);
  if (faq.details) checkText(issues, "faq.details", "常见问题 · 补充说明", faq.details, 2000, false);
  if (faq.items.length < 1 || faq.items.length > 20) {
    issues.push({ key: "faq.items.count", target: "faq.items", message: "常见问题：请保留 1 至 20 个问题。" });
  }
  faq.items.forEach((item, index) => {
    checkText(issues, `faq.${index}.question`, `常见问题 ${index + 1} · 问题`, item.question, 120);
    checkText(issues, `faq.${index}.answer`, `常见问题 ${index + 1} · 回答`, item.answer, 1000);
  });
  return issues;
}

function publishCompletenessIssues(draft: StayGuideDraft): EditorIssue[] {
  const hasVisibleCore = STANDARD_SECTIONS.some(([key]) => draft.sections[key].visible);
  return hasVisibleCore ? [] : [{
    key: "arrivalDeparture.visible.core",
    target: "arrivalDeparture.visible",
    message: CORE_VISIBILITY_MESSAGE,
  }];
}

function apiIssuePresentation(path: string): { label: string; target: string } {
  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "title") return { label: "页面标题", target: "title" };
  if (parts[0] === "intro") return { label: "简短介绍", target: "intro" };
  if (parts[0] === "sections") {
    if (parts.length === 1) return { label: "核心入住说明", target: "arrivalDeparture.visible" };
    const section = SECTION_NAMES[parts[1]] || "入住说明";
    if (parts[2] === "summary") return { label: `${section} · 简短文字`, target: `${parts[1]}.summary` };
    if (parts[2] === "details") return { label: `${section} · 补充说明`, target: `${parts[1]}.details` };
    if (parts[2] === "items") {
      const position = Number(parts[3]) + 1;
      const itemField = parts[4] === "question" ? "question" : "answer";
      return {
        label: `${section} ${Number.isFinite(position) ? position : ""} · ${itemField === "question" ? "问题" : "回答"}`,
        target: `faq.${parts[3]}.${itemField}`,
      };
    }
    if (parts[2] === "image") return { label: `${section} · 图片`, target: `${parts[1]}.image` };
    return { label: section, target: `${parts[1]}.visible` };
  }
  return { label: "通用入住说明", target: "title" };
}

function translateServerReason(reason: string): string {
  const normalized = reason.toLowerCase();
  if (normalized.includes("too long") || normalized.includes("max") || normalized.includes("length")) {
    return "内容超过允许字数，请删减后重试。";
  }
  if (normalized.includes("required") || normalized.includes("blank") || normalized.includes("empty")) {
    return "此处为必填内容，请补充后重试。";
  }
  if (normalized.includes("html") || normalized.includes("tag")) {
    return "不能包含 HTML 标签，请改为普通文字。";
  }
  if (normalized.includes("media") || normalized.includes("image")) {
    return "图片尚未通过公开媒体检查，请移除后重试或联系管理员确认。";
  }
  if (normalized.includes("privacy") || normalized.includes("password") || normalized.includes("address")) {
    return "可能包含不应公开的信息，请删除密码、门牌或住客资料后重试。";
  }
  return "未通过服务器检查，请修改此处后重试。";
}

function serverIssues(error: unknown): EditorIssue[] {
  const contentError = error as ContentClientError;
  if (Array.isArray(contentError?.issues) && contentError.issues.length) {
    return contentError.issues.map((issue) => {
      const presentation = apiIssuePresentation(issue.path);
      const isCoreVisibility = issue.path === "/sections" || issue.path === "sections";
      return {
        key: `${presentation.target}.server`,
        target: presentation.target,
        message: isCoreVisibility ? CORE_VISIBILITY_MESSAGE : `${presentation.label}：${translateServerReason(issue.reason)}`,
      };
    });
  }
  return [{ key: "title.server", target: "title", message: error instanceof Error ? error.message : "操作没有完成，请检查网络后重试。" }];
}

function idempotencyKey(prefix: string): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `${prefix}-${random}`;
}

export function StayGuideEditor(props: StayGuideEditorProps) {
  const {
    initialDraft, role, publishedVersion, conflict, activeJobId, publishJob, releases,
    releasesStatus, publishTrackingStatus, publishTrackingError, onSave, onRefresh,
    onClearConflict, onPreview, onPublish, onJobStarted, onRollback, onRetryReleases,
    onRetryPublishTracking, onNavigate,
  } = props;
  const [draft, setDraft] = useState<StayGuideDraft>(() => conflict?.localInput.draft || initialDraft.draft);
  const [revision, setRevision] = useState(initialDraft.revision);
  const [dirty, setDirty] = useState(!!conflict);
  const [saveState, setSaveState] = useState<"saved" | "unsaved" | "saving" | "failed" | "conflict">(conflict ? "conflict" : "saved");
  const [issues, setIssues] = useState<EditorIssue[]>([]);
  const [serverConflict, setServerConflict] = useState<StructuredDraftRevision<"stay_guide"> | null>(null);
  const [conflictOpen, setConflictOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(!!activeJobId);
  const [lastPreviewRevision, setLastPreviewRevision] = useState<number | null>(null);
  const [operation, setOperation] = useState<"save" | "preview" | "publish" | "history" | "rollback" | null>(null);
  const [localPublishActive, setLocalPublishActive] = useState(false);
  const draftRef = useRef(draft);
  const revisionRef = useRef(revision);
  const dirtyRef = useRef(dirty);
  const generationRef = useRef(0);
  const serverDraftRef = useRef(initialDraft.draft);
  const operationRef = useRef<typeof operation>(null);
  const publishActiveRef = useRef(!!activeJobId);
  const savePromiseRef = useRef<Promise<StructuredDraftRevision<"stay_guide">> | null>(null);
  const conflictDialogRef = useRef<HTMLDivElement>(null);
  const conflictPrimaryRef = useRef<HTMLButtonElement>(null);

  const closeConflictDialog = useCallback(() => setConflictOpen(false), []);
  useModalDialog({
    open: conflictOpen && !!serverConflict,
    dialogRef: conflictDialogRef,
    initialFocusRef: conflictPrimaryRef,
    onClose: closeConflictDialog,
    closeDisabled: operation === "save",
  });

  useEffect(() => {
    publishActiveRef.current = !!activeJobId || localPublishActive;
    if (activeJobId) setPublishOpen(true);
  }, [activeJobId, localPublishActive]);
  useEffect(() => {
    if (conflict) {
      setSaveState("conflict");
      dirtyRef.current = true;
      setDirty(true);
    }
  }, [conflict]);
  useEffect(() => {
    if (publishJob && isStructuredContentTerminalJobStatus(publishJob.status)) {
      publishActiveRef.current = !!activeJobId;
      setLocalPublishActive(false);
    }
  }, [activeJobId, publishJob]);

  const updateDraft = (next: StayGuideDraft) => {
    draftRef.current = next;
    generationRef.current += 1;
    dirtyRef.current = true;
    setDraft(next);
    setDirty(true);
    setSaveState("unsaved");
    setIssues([]);
  };

  const performSave = async (
    expectedRevision = revisionRef.current,
    sourceDraft = draftRef.current,
  ): Promise<StructuredDraftRevision<"stay_guide">> => {
    if (savePromiseRef.current) return savePromiseRef.current;
    const nextIssues = validateDraft(sourceDraft);
    if (nextIssues.length) {
      setIssues(nextIssues);
      setSaveState("failed");
      throw new Error(nextIssues[0].message);
    }
    const cleaned = sanitizeDraft(sourceDraft);
    const snapshotGeneration = generationRef.current;
    setSaveState("saving");
    const request = (async () => {
      try {
        const saved = await onSave({ expectedRevision, draft: cleaned });
        serverDraftRef.current = saved.draft;
        revisionRef.current = saved.revision;
        setRevision(saved.revision);
        setIssues([]);
        setConflictOpen(false);
        setServerConflict(null);
        onClearConflict();
        if (generationRef.current === snapshotGeneration) {
          draftRef.current = saved.draft;
          dirtyRef.current = false;
          setDraft(saved.draft);
          setDirty(false);
          setSaveState("saved");
        } else {
          dirtyRef.current = true;
          setDirty(true);
          setSaveState("unsaved");
        }
        return saved;
      } catch (error) {
        if ((error as ContentClientError)?.status === 409 || conflict) setSaveState("conflict");
        else setSaveState("failed");
        setIssues(serverIssues(error));
        throw error;
      } finally {
        savePromiseRef.current = null;
      }
    })();
    savePromiseRef.current = request;
    return request;
  };

  const runExclusive = async <T,>(kind: "save" | "preview" | "publish", action: () => Promise<T>): Promise<T | undefined> => {
    if (operationRef.current || publishActiveRef.current) return undefined;
    operationRef.current = kind;
    setOperation(kind);
    try {
      return await action();
    } finally {
      operationRef.current = null;
      setOperation(null);
    }
  };

  const staleActionError = () => {
    const issue = {
      key: "title.stale",
      target: "title",
      message: "保存期间内容又有修改，请先保存最新内容后再预览或发布。",
    };
    setIssues([issue]);
    throw new Error(issue.message);
  };

  const saveCurrent = () => runExclusive("save", () => performSave());

  const flushSave = async (): Promise<StructuredDraftRevision<"stay_guide">> => {
    if (savePromiseRef.current) await savePromiseRef.current;
    while (dirtyRef.current) await performSave(revisionRef.current, draftRef.current);
    return {
      ...initialDraft,
      revision: revisionRef.current,
      draft: serverDraftRef.current,
    };
  };

  const requestPreview = () => runExclusive("preview", async () => {
    const actionGeneration = generationRef.current;
    const saved = dirtyRef.current ? await performSave() : {
      ...initialDraft,
      revision: revisionRef.current,
      draft: serverDraftRef.current,
    };
    if (dirtyRef.current || generationRef.current !== actionGeneration) return staleActionError();
    try {
      const result = await onPreview(saved.revision);
      if (dirtyRef.current || generationRef.current !== actionGeneration) return staleActionError();
      setLastPreviewRevision(result.revision);
      return result;
    } catch (error) {
      setIssues(serverIssues(error));
      throw error;
    }
  });

  const requestPublish = () => runExclusive("publish", async () => {
    const completenessIssues = publishCompletenessIssues(draftRef.current);
    if (completenessIssues.length) {
      setIssues(completenessIssues);
      throw new Error(completenessIssues[0].message);
    }
    const actionGeneration = generationRef.current;
    const saved = dirtyRef.current ? await performSave() : {
      ...initialDraft,
      revision: revisionRef.current,
      draft: serverDraftRef.current,
    };
    if (dirtyRef.current || generationRef.current !== actionGeneration) return staleActionError();
    try {
      await onPreview(saved.revision);
      if (dirtyRef.current || generationRef.current !== actionGeneration) return staleActionError();
      const accepted = await onPublish({ revision: saved.revision, idempotencyKey: idempotencyKey("stay-publish") });
      publishActiveRef.current = true;
      setLocalPublishActive(true);
      onJobStarted(accepted);
      setPublishOpen(true);
    } catch (error) {
      setIssues(serverIssues(error));
      throw error;
    }
  });

  const openHistory = () => {
    if (operationRef.current || publishActiveRef.current) return;
    operationRef.current = "history";
    setOperation("history");
    setHistoryOpen(true);
  };

  const closeHistory = () => {
    setHistoryOpen(false);
    if (operationRef.current === "history") {
      operationRef.current = null;
      setOperation(null);
    }
  };

  const requestRollback = async (version: string) => {
    if (operationRef.current !== "history" || publishActiveRef.current) {
      throw new Error("当前有其他操作正在进行，请完成后再重试回滚。");
    }
    operationRef.current = "rollback";
    setOperation("rollback");
    try {
      const accepted = await onRollback({ version, idempotencyKey: idempotencyKey("stay-rollback") });
      publishActiveRef.current = true;
      setLocalPublishActive(true);
      onJobStarted(accepted);
      setPublishOpen(true);
    } catch (error) {
      if (!publishActiveRef.current) {
        operationRef.current = "history";
        setOperation("history");
      }
      throw error;
    } finally {
      if (operationRef.current === "rollback") {
        operationRef.current = null;
        setOperation(null);
      }
    }
  };

  const compareConflict = async () => {
    const server = await onRefresh();
    setServerConflict(server);
    setConflictOpen(true);
  };

  const useServerDraft = () => {
    if (!serverConflict) return;
    draftRef.current = serverConflict.draft;
    serverDraftRef.current = serverConflict.draft;
    revisionRef.current = serverConflict.revision;
    dirtyRef.current = false;
    setDraft(serverConflict.draft);
    setRevision(serverConflict.revision);
    setDirty(false);
    setSaveState("saved");
    setIssues([]);
    setConflictOpen(false);
    setServerConflict(null);
    onClearConflict();
  };

  const completenessIssues = useMemo(() => publishCompletenessIssues(draft), [draft]);
  const displayedIssues = useMemo(() => {
    const unique = new Map<string, EditorIssue>();
    [...issues, ...completenessIssues].forEach((issue) => unique.set(issue.key, issue));
    return Array.from(unique.values());
  }, [completenessIssues, issues]);
  const readyForPublish = useMemo(() => validateDraft(draft).length === 0 && completenessIssues.length === 0, [completenessIssues.length, draft]);
  const jobInProgress = localPublishActive || !!activeJobId;
  const operationBlocked = operation !== null || jobInProgress;
  const focusIssue = (issue: EditorIssue) => {
    const target = document.getElementById(fieldId(issue.target));
    target?.scrollIntoView?.({ block: "center" });
    target?.focus();
  };
  const fieldAccessibility = (target: string) => {
    const targetIssues = displayedIssues.filter((issue) => issue.target === target);
    return {
      id: fieldId(target),
      "aria-invalid": targetIssues.length ? true : undefined,
      "aria-describedby": targetIssues.length ? targetIssues.map(errorId).join(" ") : undefined,
    };
  };
  const visibleCount = [...STANDARD_SECTIONS.map(([key]) => draft.sections[key]), draft.sections.faq].filter((section) => section.visible).length;
  const actions: ContentEditorAction[] = [
    ...(saveState === "conflict" ? [{ key: "conflict", label: "查看服务器最新内容", kind: "primary" as const, onClick: compareConflict }] : []),
    { key: "save", label: "保存当前内容", kind: dirty && saveState !== "conflict" ? "primary" : "secondary", onClick: saveCurrent, disabled: saveState === "conflict" || operationBlocked, busy: operation === "save" },
    { key: "preview", label: "检查手机预览", kind: role === "operator" && !dirty ? "primary" : "secondary", onClick: requestPreview, disabled: saveState === "conflict" || operationBlocked },
    { key: "history", label: "查看历史版本", kind: "secondary", onClick: openHistory, disabled: operationBlocked },
    { key: "publish", label: "发布本次修改", kind: dirty ? "secondary" : "primary", onClick: requestPublish, adminOnly: true, disabled: saveState === "conflict" || !readyForPublish || operationBlocked },
  ];

  const field = (key: typeof STANDARD_SECTIONS[number][0]) => {
    const section = draft.sections[key];
    const label = SECTION_NAMES[key];
    const prompt = STANDARD_SECTIONS.find(([sectionKey]) => sectionKey === key)?.[2];
    return (
      <fieldset key={key} className={styles.stayGuideSection}>
        <legend>{label}</legend>
        <p className={styles.stayGuidePrompt}>{prompt}</p>
        <label className={styles.stayGuideToggle}>
          <input {...fieldAccessibility(`${key}.visible`)} type="checkbox" checked={section.visible} aria-label={`在小程序显示${label}`} onChange={(event) => updateDraft({ ...draft, sections: { ...draft.sections, [key]: { ...section, visible: event.target.checked } } })} />
          在小程序中显示
        </label>
        <label className={styles.stayGuideField}>
          <span>简短文字</span>
          <textarea {...fieldAccessibility(`${key}.summary`)} aria-label={`${label}简短文字`} value={section.summary} onChange={(event) => updateDraft({ ...draft, sections: { ...draft.sections, [key]: { ...section, summary: event.target.value } } })} />
          <small>必填，最多 240 字</small>
        </label>
        <label className={styles.stayGuideField}>
          <span>补充说明（可选）</span>
          <textarea {...fieldAccessibility(`${key}.details`)} aria-label={`${label}补充说明`} value={section.details || ""} onChange={(event) => updateDraft({ ...draft, sections: { ...draft.sections, [key]: { ...section, details: event.target.value || undefined } } })} />
          <small>最多 2000 字，只填写所有住客都能公开看到的通用信息</small>
        </label>
        <div {...fieldAccessibility(`${key}.image`)} className={styles.stayGuideImageField} tabIndex={-1}>
          <strong>图片（可选）</strong>
          {section.image ? <><span>已保留经过媒体审核的公开图片</span><button type="button" onClick={() => { const { image: _image, ...rest } = section; updateDraft({ ...draft, sections: { ...draft.sections, [key]: rest } }); }}>移除图片</button></> : <span>本页暂不提供新图片上传；保存不会改动其他内容的图片。</span>}
        </div>
      </fieldset>
    );
  };

  return (
    <>
      <ContentEditorShell
        title="通用入住说明"
        explanation="编辑所有住客都能查看的公共入住与离店说明。"
        statusLabel={saveState === "conflict" ? "内容冲突" : dirty ? "有未保存修改" : publishedVersion ? "内容已保存" : "内容未发布"}
        statusExplanation={saveState === "conflict" ? "服务器内容已有更新，本地修改仍保留。请先查看并选择版本。" : `当前显示 ${visibleCount} 个栏目。`}
        completion={`已填写 ${visibleCount} / 7 个公开栏目`}
        nextStep={saveState === "conflict" ? "解决内容冲突" : dirty ? "保存当前内容" : role === "admin" ? "检查并发布" : "联系管理员发布"}
        role={role}
        saveState={saveState}
        hasUnsavedChanges={dirty || saveState === "conflict"}
        readyForPublish={readyForPublish}
        onSave={flushSave}
        onNavigate={onNavigate}
        actions={actions}
        form={(
          <div className={styles.stayGuideForm}>
            <section className={styles.stayGuidePublicWarning} role="note">
              <strong>这是所有住客都能看到的公开通用说明</strong>
              <p>不要填写房间密码、门锁码、客人资料、精确房间位置、门牌号或房源专属进入方式。</p>
            </section>
            {conflict ? (
              <section className={styles.persistentError} role="alert">
                <strong>服务器内容已有更新</strong>
                <span>本地修改仍保留。点击“查看服务器最新内容”比较后，再选择保留哪一份。</span>
              </section>
            ) : null}
            {displayedIssues.length ? (
              <section className={styles.persistentError} role="alert" aria-label="内容检查问题">
                <strong>请处理以下内容后再保存或发布</strong>
                <ol>{displayedIssues.map((issue) => (
                  <li id={errorId(issue)} key={issue.key}>
                    <button type="button" className={styles.stayGuideIssueButton} onClick={() => focusIssue(issue)}>{issue.message}</button>
                  </li>
                ))}</ol>
              </section>
            ) : null}
            <label className={styles.stayGuideField}>
              <span>页面标题</span>
              <input {...fieldAccessibility("title")} aria-label="页面标题" value={draft.title} onChange={(event) => updateDraft({ ...draft, title: event.target.value })} />
              <small>必填，最多 80 字</small>
            </label>
            <label className={styles.stayGuideField}>
              <span>简短介绍</span>
              <textarea {...fieldAccessibility("intro")} aria-label="简短介绍" value={draft.intro} onChange={(event) => updateDraft({ ...draft, intro: event.target.value })} />
              <small>必填，最多 240 字</small>
            </label>
            {STANDARD_SECTIONS.map(([key]) => field(key))}
            <fieldset className={styles.stayGuideSection}>
              <legend>常见问题</legend>
              <label className={styles.stayGuideToggle}>
                <input {...fieldAccessibility("faq.visible")} type="checkbox" checked={draft.sections.faq.visible} aria-label="在小程序显示常见问题" onChange={(event) => updateDraft({ ...draft, sections: { ...draft.sections, faq: { ...draft.sections.faq, visible: event.target.checked } } })} />
                在小程序中显示
              </label>
              <label className={styles.stayGuideField}>
                <span>简短文字</span>
                <textarea {...fieldAccessibility("faq.summary")} aria-label="常见问题简短文字" value={draft.sections.faq.summary} onChange={(event) => updateDraft({ ...draft, sections: { ...draft.sections, faq: { ...draft.sections.faq, summary: event.target.value } } })} />
              </label>
              <label className={styles.stayGuideField}>
                <span>补充说明（可选）</span>
                <textarea {...fieldAccessibility("faq.details")} aria-label="常见问题补充说明" value={draft.sections.faq.details || ""} onChange={(event) => updateDraft({ ...draft, sections: { ...draft.sections, faq: { ...draft.sections.faq, details: event.target.value || undefined } } })} />
              </label>
              <div {...fieldAccessibility("faq.image")} className={styles.stayGuideImageField} tabIndex={-1}>
                <strong>图片（可选）</strong>
                {draft.sections.faq.image ? <><span>已保留经过媒体审核的公开图片</span><button type="button" aria-label="移除常见问题图片" onClick={() => { const { image: _image, ...faq } = draft.sections.faq; updateDraft({ ...draft, sections: { ...draft.sections, faq } }); }}>移除图片</button></> : <span>本页暂不提供新图片上传；保存不会改动其他内容的图片。</span>}
              </div>
              <ol className={styles.stayGuideFaqList}>
                {draft.sections.faq.items.map((item, index) => (
                  <li key={index}>
                    <label className={styles.stayGuideField}><span>问题 {index + 1}</span><input {...fieldAccessibility(`faq.${index}.question`)} aria-label={`第 ${index + 1} 个问题`} value={item.question} onChange={(event) => { const items = draft.sections.faq.items.map((entry, itemIndex) => itemIndex === index ? { ...entry, question: event.target.value } : entry); updateDraft({ ...draft, sections: { ...draft.sections, faq: { ...draft.sections.faq, items } } }); }} /></label>
                    <label className={styles.stayGuideField}><span>回答</span><textarea {...fieldAccessibility(`faq.${index}.answer`)} aria-label={`第 ${index + 1} 个回答`} value={item.answer} onChange={(event) => { const items = draft.sections.faq.items.map((entry, itemIndex) => itemIndex === index ? { ...entry, answer: event.target.value } : entry); updateDraft({ ...draft, sections: { ...draft.sections, faq: { ...draft.sections.faq, items } } }); }} /></label>
                    <div className={styles.stayGuideFaqActions}>
                      <button type="button" disabled={index === 0} aria-label={`上移第 ${index + 1} 个常见问题`} onClick={() => { const items = [...draft.sections.faq.items]; [items[index - 1], items[index]] = [items[index], items[index - 1]]; updateDraft({ ...draft, sections: { ...draft.sections, faq: { ...draft.sections.faq, items } } }); }}>上移</button>
                      <button type="button" disabled={index === draft.sections.faq.items.length - 1} aria-label={`下移第 ${index + 1} 个常见问题`} onClick={() => { const items = [...draft.sections.faq.items]; [items[index], items[index + 1]] = [items[index + 1], items[index]]; updateDraft({ ...draft, sections: { ...draft.sections, faq: { ...draft.sections.faq, items } } }); }}>下移</button>
                      <button type="button" disabled={draft.sections.faq.items.length === 1} aria-label={`删除第 ${index + 1} 个常见问题`} onClick={() => updateDraft({ ...draft, sections: { ...draft.sections, faq: { ...draft.sections.faq, items: draft.sections.faq.items.filter((_, itemIndex) => itemIndex !== index) } } })}>删除</button>
                    </div>
                  </li>
                ))}
              </ol>
              <button {...fieldAccessibility("faq.items")} type="button" className={styles.editorSecondaryAction} disabled={draft.sections.faq.items.length >= 20} onClick={() => updateDraft({ ...draft, sections: { ...draft.sections, faq: { ...draft.sections.faq, items: [...draft.sections.faq.items, { question: "", answer: "" }] } } })}>新增常见问题</button>
            </fieldset>
          </div>
        )}
        renderPreview={(width: ContentPreviewWidth) => (
          <div>
            <MobileStayGuidePreview draft={draft} width={width} />
            {lastPreviewRevision !== null ? <p className={styles.durableNote}>服务器已检查当前内容版本 {lastPreviewRevision}</p> : null}
          </div>
        )}
        technicalDetails={<><p>草稿内容版本：{revision}</p><p>线上发布版本：{publishedVersion || "尚未发布"}</p>{activeJobId ? <p>当前发布任务：{activeJobId}</p> : null}</>}
      />

      {conflictOpen && serverConflict ? (
        <div className={styles.overlay}>
          <div ref={conflictDialogRef} className={styles.confirmationDialog} role="dialog" aria-modal="true" aria-label="解决内容冲突" tabIndex={-1}>
            <h2>解决内容冲突</h2>
            <p>服务器标题：{serverConflict.draft.title}</p>
            <p>本地标题：{draft.title}</p>
            <div className={styles.dialogActions}>
              <button ref={conflictPrimaryRef} type="button" className={styles.editorSecondaryAction} disabled={operation === "save"} onClick={useServerDraft}>使用服务器版本</button>
              <button type="button" className={styles.editorPrimaryAction} disabled={operation === "save"} onClick={() => void runExclusive("save", () => performSave(serverConflict.revision, draftRef.current))}>以本地内容重新保存</button>
            </div>
          </div>
        </div>
      ) : null}
      <PublishDialog
        open={publishOpen}
        job={publishJob}
        onClose={() => setPublishOpen(false)}
        trackingStatus={publishTrackingStatus}
        trackingError={publishTrackingError}
        onRetryTracking={onRetryPublishTracking}
        onRetry={() => void requestPublish()}
        contentTitle="通用入住说明"
        isAdmin={role === "admin"}
      />
      <ReleaseHistoryDrawer
        open={historyOpen}
        releases={releases}
        canRollback={role === "admin"}
        onClose={closeHistory}
        status={releasesStatus}
        onRetry={onRetryReleases}
        actionsDisabled={operation !== "history" || jobInProgress}
        onRollback={requestRollback}
      />
    </>
  );
}
