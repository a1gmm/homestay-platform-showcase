"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ContentClientError,
  ContentRelease,
  PublishJob,
  PublishJobAccepted,
  RollbackStructuredReleaseInput,
  SaveStructuredDraftInput,
  StructuredDraftRevision,
  StructuredGuidePreview,
  TravelGuideDraft,
} from "@/features/content/types";
import { isStructuredContentTerminalJobStatus, type StructuredDraftConflict } from "@/hooks/content/useStructuredContent";
import { useModalDialog } from "@/hooks/useModalDialog";
import { ContentEditorShell, type ContentEditorAction, type ContentEditorRole, type ContentPreviewWidth } from "./ContentEditorShell";
import { MobileTravelGuidePreview, TRAVEL_CATEGORY_OPTIONS } from "./MobileTravelGuidePreview";
import { PublishDialog } from "./PublishDialog";
import { ReleaseHistoryDrawer } from "./ReleaseHistoryDrawer";
import styles from "./content-center.module.css";

type Recommendation = TravelGuideDraft["recommendations"][number];
type Category = Recommendation["category"];

interface EditorIssue { key: string; target: string; message: string }

export interface TravelGuideEditorProps {
  initialDraft: StructuredDraftRevision<"travel">;
  role: ContentEditorRole;
  publishedVersion: string | null;
  conflict: StructuredDraftConflict<"travel"> | null;
  activeJobId: string | null;
  publishJob: PublishJob | null;
  releases: ContentRelease[];
  releasesStatus: "loading" | "success" | "error";
  publishTrackingStatus: "idle" | "loading" | "success" | "error";
  publishTrackingError?: string | null;
  onSave(input: SaveStructuredDraftInput<"travel">): Promise<StructuredDraftRevision<"travel">>;
  onRefresh(): Promise<StructuredDraftRevision<"travel">>;
  onClearConflict(): void;
  onPreview(revision: number): Promise<StructuredGuidePreview<"travel">>;
  onPublish(input: { revision: number; idempotencyKey: string }): Promise<PublishJobAccepted>;
  onJobStarted(job: PublishJobAccepted): void;
  onRollback(input: RollbackStructuredReleaseInput): Promise<PublishJobAccepted>;
  onRetryReleases?(): void;
  onRetryPublishTracking?(): void;
  onNavigate?(href: string): void;
}

const normalizeText = (value: string) => value.replace(/\r\n?/g, "\n").trim();
const cleanOptional = (value?: string) => normalizeText(value || "") || undefined;
const containsHtml = (value: string) => /<\/?[a-z][^>]*>/i.test(value);
const fieldId = (target: string) => `travel-guide-${target.replace(/\./g, "-")}`;
const errorId = (issue: EditorIssue) => `travel-guide-error-${issue.key.replace(/\./g, "-")}`;
const recommendationTarget = (index: number, field: string) => `recommendations.${index}.${field}`;

function parseAudiences(value: string): string[] {
  return value.replace(/\r\n?/g, "\n").split(/[\n,，、]+/).map((item) => item.trim()).filter(Boolean);
}

function sanitizeDraft(draft: TravelGuideDraft): TravelGuideDraft {
  return {
    title: normalizeText(draft.title),
    intro: normalizeText(draft.intro),
    recommendations: draft.recommendations.map((item) => {
      const approximateLocation = cleanOptional(item.approximateLocation);
      const suggestedDuration = cleanOptional(item.suggestedDuration);
      return {
        id: item.id,
        name: normalizeText(item.name),
        category: item.category,
        reason: normalizeText(item.reason),
        ...(approximateLocation ? { approximateLocation } : {}),
        ...(suggestedDuration ? { suggestedDuration } : {}),
        audiences: item.audiences.map(normalizeText).filter(Boolean),
        ...(item.image ? { image: item.image } : {}),
        visible: item.visible,
      };
    }),
  };
}

function checkText(issues: EditorIssue[], target: string, label: string, value: string, maximum: number, required = true) {
  const cleaned = normalizeText(value);
  if (required && !cleaned) issues.push({ key: `${target}.required`, target, message: `${label}：请填写内容。` });
  else if (cleaned.length > maximum) issues.push({ key: `${target}.maximum`, target, message: `${label}：最多 ${maximum} 字，请删减后再保存。` });
  if (containsHtml(value)) issues.push({ key: `${target}.html`, target, message: `${label}：不能包含 HTML 标签，请改为普通文字。` });
}

function validateDraft(draft: TravelGuideDraft): EditorIssue[] {
  const issues: EditorIssue[] = [];
  checkText(issues, "title", "页面标题", draft.title, 80);
  checkText(issues, "intro", "简短介绍", draft.intro, 240);
  if (draft.recommendations.length < 1 || draft.recommendations.length > 50) issues.push({ key: "recommendations.count", target: "recommendations", message: "推荐内容：请保留 1 至 50 条推荐。" });
  const ids = new Set<string>();
  let duplicated = false;
  draft.recommendations.forEach((item, index) => {
    if (ids.has(item.id)) duplicated = true;
    ids.add(item.id);
    const prefix = `第 ${index + 1} 条推荐`;
    checkText(issues, recommendationTarget(index, "name"), `${prefix} · 名称`, item.name, 80);
    checkText(issues, recommendationTarget(index, "reason"), `${prefix} · 推荐理由`, item.reason, 240);
    if (item.approximateLocation) checkText(issues, recommendationTarget(index, "approximateLocation"), `${prefix} · 大概位置`, item.approximateLocation, 120, false);
    if (item.suggestedDuration) checkText(issues, recommendationTarget(index, "suggestedDuration"), `${prefix} · 建议时长`, item.suggestedDuration, 80, false);
    if (item.audiences.length < 1 || item.audiences.length > 6) issues.push({ key: `${index}.audiences.count`, target: recommendationTarget(index, "audiences"), message: `${prefix} · 适合人群：请填写 1 至 6 类人群。` });
    item.audiences.forEach((audience, audienceIndex) => checkText(issues, recommendationTarget(index, "audiences"), `${prefix} · 适合人群 ${audienceIndex + 1}`, audience, 40));
  });
  if (duplicated) issues.push({ key: "recommendations.ids", target: "recommendations", message: "推荐条目内部标识重复，请删除重复条目后重试。" });
  return issues;
}

function completenessIssues(draft: TravelGuideDraft): EditorIssue[] {
  return draft.recommendations.some((item) => item.visible) ? [] : [{ key: "recommendations.visible", target: "recommendations.0.visible", message: "至少保留一条在小程序中显示的推荐。" }];
}

function serverIssues(error: unknown): EditorIssue[] {
  const contentError = error as ContentClientError;
  if (!Array.isArray(contentError?.issues) || !contentError.issues.length) return [{ key: "title.server", target: "title", message: error instanceof Error ? error.message : "操作没有完成，请检查网络后重试。" }];
  return contentError.issues.map((issue) => {
    const parts = issue.path.split("/").filter(Boolean);
    if (parts[0] !== "recommendations") return { key: `${parts[0] || "title"}.server`, target: parts[0] || "title", message: "内容未通过服务器检查，请修改后重试。" };
    if (parts.length === 1) return { key: "recommendations.server", target: "recommendations.0.visible", message: "至少保留一条在小程序中显示的推荐。" };
    const index = Number(parts[1]);
    const field = parts[2] || "name";
    const labels: Record<string, string> = { name: "名称", reason: "推荐理由", approximateLocation: "大概位置", suggestedDuration: "建议时长", audiences: "适合人群", image: "图片", category: "分类", visible: "是否显示" };
    return { key: `${index}.${field}.server`, target: recommendationTarget(index, field), message: `第 ${index + 1} 条推荐 · ${labels[field] || "内容"}：未通过服务器检查，请修改后重试。` };
  });
}

function uniqueId(items: Recommendation[]): Recommendation["id"] {
  const used = new Set(items.map((item) => item.id));
  let value = `travel-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  while (used.has(value as Recommendation["id"])) value = `${value}-x`;
  return value as Recommendation["id"];
}

function idempotencyKey(prefix: string) {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `${prefix}-${random}`;
}

export function TravelGuideEditor(props: TravelGuideEditorProps) {
  const { initialDraft, role, publishedVersion, conflict, activeJobId, publishJob, releases, releasesStatus, publishTrackingStatus, publishTrackingError, onSave, onRefresh, onClearConflict, onPreview, onPublish, onJobStarted, onRollback, onRetryReleases, onRetryPublishTracking, onNavigate } = props;
  const initialLocal = conflict?.localInput.draft || initialDraft.draft;
  const [draft, setDraft] = useState<TravelGuideDraft>(initialLocal);
  const [revision, setRevision] = useState(initialDraft.revision);
  const [dirty, setDirty] = useState(!!conflict);
  const [saveState, setSaveState] = useState<"saved" | "unsaved" | "saving" | "failed" | "conflict">(conflict ? "conflict" : "saved");
  const [issues, setIssues] = useState<EditorIssue[]>([]);
  const [serverConflict, setServerConflict] = useState<StructuredDraftRevision<"travel"> | null>(null);
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
  const savePromiseRef = useRef<Promise<StructuredDraftRevision<"travel">> | null>(null);
  const conflictDialogRef = useRef<HTMLDivElement>(null);
  const conflictPrimaryRef = useRef<HTMLButtonElement>(null);
  const closeConflictDialog = useCallback(() => setConflictOpen(false), []);

  useModalDialog({ open: conflictOpen && !!serverConflict, dialogRef: conflictDialogRef, initialFocusRef: conflictPrimaryRef, onClose: closeConflictDialog, closeDisabled: operation === "save" });
  useEffect(() => { publishActiveRef.current = !!activeJobId || localPublishActive; if (activeJobId) setPublishOpen(true); }, [activeJobId, localPublishActive]);
  useEffect(() => { if (conflict) { setSaveState("conflict"); dirtyRef.current = true; setDirty(true); } }, [conflict]);
  useEffect(() => { if (publishJob && isStructuredContentTerminalJobStatus(publishJob.status)) { publishActiveRef.current = !!activeJobId; setLocalPublishActive(false); } }, [activeJobId, publishJob]);

  const updateDraft = (next: TravelGuideDraft) => { draftRef.current = next; generationRef.current += 1; dirtyRef.current = true; setDraft(next); setDirty(true); setSaveState("unsaved"); setIssues([]); };

  const performSave = async (expectedRevision = revisionRef.current, sourceDraft = draftRef.current) => {
    if (savePromiseRef.current) return savePromiseRef.current;
    const nextIssues = validateDraft(sourceDraft);
    if (nextIssues.length) { setIssues(nextIssues); setSaveState("failed"); throw new Error(nextIssues[0].message); }
    const cleaned = sanitizeDraft(sourceDraft);
    const snapshotGeneration = generationRef.current;
    setSaveState("saving");
    const request = (async () => {
      try {
        const saved = await onSave({ expectedRevision, draft: cleaned });
        serverDraftRef.current = saved.draft; revisionRef.current = saved.revision; setRevision(saved.revision); setIssues([]); setConflictOpen(false); setServerConflict(null); onClearConflict();
        if (generationRef.current === snapshotGeneration) { draftRef.current = saved.draft; dirtyRef.current = false; setDraft(saved.draft); setDirty(false); setSaveState("saved"); }
        else { dirtyRef.current = true; setDirty(true); setSaveState("unsaved"); }
        return saved;
      } catch (error) { setSaveState((error as ContentClientError)?.status === 409 || conflict ? "conflict" : "failed"); setIssues(serverIssues(error)); throw error; }
      finally { savePromiseRef.current = null; }
    })();
    savePromiseRef.current = request;
    return request;
  };

  const runExclusive = async <T,>(kind: "save" | "preview" | "publish", action: () => Promise<T>): Promise<T | undefined> => {
    if (operationRef.current || publishActiveRef.current) return undefined;
    operationRef.current = kind; setOperation(kind);
    try { return await action(); } finally { operationRef.current = null; setOperation(null); }
  };
  const staleActionError = () => { const issue = { key: "title.stale", target: "title", message: "保存期间内容又有修改，请先保存最新内容后再预览或发布。" }; setIssues([issue]); throw new Error(issue.message); };
  const saveCurrent = () => runExclusive("save", () => performSave());
  const flushSave = async () => { if (savePromiseRef.current) await savePromiseRef.current; while (dirtyRef.current) await performSave(revisionRef.current, draftRef.current); return { ...initialDraft, revision: revisionRef.current, draft: serverDraftRef.current }; };
  const requestPreview = () => runExclusive("preview", async () => {
    const generation = generationRef.current;
    const saved = dirtyRef.current ? await performSave() : { ...initialDraft, revision: revisionRef.current, draft: serverDraftRef.current };
    if (dirtyRef.current || generationRef.current !== generation) return staleActionError();
    try { const result = await onPreview(saved.revision); if (dirtyRef.current || generationRef.current !== generation) return staleActionError(); setLastPreviewRevision(result.revision); return result; } catch (error) { setIssues(serverIssues(error)); throw error; }
  });
  const requestPublish = () => runExclusive("publish", async () => {
    const readyIssues = completenessIssues(draftRef.current); if (readyIssues.length) { setIssues(readyIssues); throw new Error(readyIssues[0].message); }
    const generation = generationRef.current;
    const saved = dirtyRef.current ? await performSave() : { ...initialDraft, revision: revisionRef.current, draft: serverDraftRef.current };
    if (dirtyRef.current || generationRef.current !== generation) return staleActionError();
    try { await onPreview(saved.revision); if (dirtyRef.current || generationRef.current !== generation) return staleActionError(); const accepted = await onPublish({ revision: saved.revision, idempotencyKey: idempotencyKey("travel-publish") }); publishActiveRef.current = true; setLocalPublishActive(true); onJobStarted(accepted); setPublishOpen(true); }
    catch (error) { setIssues(serverIssues(error)); throw error; }
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
      const accepted = await onRollback({ version, idempotencyKey: idempotencyKey("travel-rollback") });
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

  const compareConflict = async () => { const server = await onRefresh(); setServerConflict(server); setConflictOpen(true); };
  const useServerDraft = () => { if (!serverConflict) return; draftRef.current = serverConflict.draft; serverDraftRef.current = serverConflict.draft; revisionRef.current = serverConflict.revision; dirtyRef.current = false; setDraft(serverConflict.draft); setRevision(serverConflict.revision); setDirty(false); setSaveState("saved"); setIssues([]); setConflictOpen(false); setServerConflict(null); onClearConflict(); };

  const requiredIssues = useMemo(() => completenessIssues(draft), [draft]);
  const displayedIssues = useMemo(() => { const unique = new Map<string, EditorIssue>(); [...issues, ...requiredIssues].forEach((issue) => unique.set(issue.key, issue)); return Array.from(unique.values()); }, [issues, requiredIssues]);
  const readyForPublish = useMemo(() => validateDraft(draft).length === 0 && requiredIssues.length === 0, [draft, requiredIssues.length]);
  const jobInProgress = localPublishActive || !!activeJobId;
  const operationBlocked = operation !== null || jobInProgress;
  const fieldAccessibility = (target: string) => { const matches = displayedIssues.filter((issue) => issue.target === target); return { id: fieldId(target), "aria-invalid": matches.length ? true : undefined, "aria-describedby": matches.length ? matches.map(errorId).join(" ") : undefined }; };
  const focusIssue = (issue: EditorIssue) => { const target = document.getElementById(fieldId(issue.target)); target?.scrollIntoView?.({ block: "center" }); target?.focus(); };
  const actions: ContentEditorAction[] = [
    ...(saveState === "conflict" ? [{ key: "conflict", label: "查看服务器最新内容", kind: "primary" as const, onClick: compareConflict }] : []),
    { key: "save", label: "保存当前内容", kind: dirty && saveState !== "conflict" ? "primary" : "secondary", onClick: saveCurrent, disabled: saveState === "conflict" || operationBlocked, busy: operation === "save" },
    { key: "preview", label: "检查手机预览", kind: role === "operator" && !dirty ? "primary" : "secondary", onClick: requestPreview, disabled: saveState === "conflict" || operationBlocked },
    { key: "history", label: "查看历史版本", kind: "secondary", onClick: openHistory, disabled: operationBlocked },
    { key: "publish", label: "发布本次修改", kind: dirty ? "secondary" : "primary", onClick: requestPublish, adminOnly: true, disabled: saveState === "conflict" || !readyForPublish || operationBlocked },
  ];

  const changeItem = (index: number, patch: Partial<Recommendation>) => updateDraft({ ...draft, recommendations: draft.recommendations.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item) });
  const moveItem = (index: number, offset: -1 | 1) => { const items = [...draft.recommendations]; [items[index], items[index + offset]] = [items[index + offset], items[index]]; updateDraft({ ...draft, recommendations: items }); };

  return <>
    <ContentEditorShell
      title="灵山湾旅游攻略"
      explanation="维护住客都能看到的灵山湾公开通用推荐。"
      statusLabel={saveState === "conflict" ? "内容冲突" : dirty ? "有未保存修改" : publishedVersion ? "内容已保存" : "内容未发布"}
      statusExplanation={saveState === "conflict" ? "服务器内容已有更新，本地修改仍保留。请先查看并选择版本。" : `当前显示 ${draft.recommendations.filter((item) => item.visible).length} 条推荐。`}
      completion={`已填写 ${draft.recommendations.length} 条推荐`}
      nextStep={saveState === "conflict" ? "解决内容冲突" : dirty ? "保存当前内容" : role === "admin" ? "检查并发布" : "联系管理员发布"}
      role={role} saveState={saveState} hasUnsavedChanges={dirty || saveState === "conflict"} readyForPublish={readyForPublish} onSave={flushSave} onNavigate={onNavigate} actions={actions}
      form={<div className={styles.stayGuideForm}>
        <section className={styles.stayGuidePublicWarning} role="note"><strong>这是所有住客都能看到的公开通用攻略</strong><p>只填写普遍适用的建议；营业时间、开放状态、交通和停车情况可能变化，请提醒住客出发前再次确认。</p></section>
        {conflict ? <section className={styles.persistentError} role="alert"><strong>服务器内容已有更新</strong><span>本地修改仍保留。请先查看服务器最新内容。</span></section> : null}
        {displayedIssues.length ? <section className={styles.persistentError} role="alert" aria-label="内容检查问题"><strong>请处理以下内容后再保存或发布</strong><ol>{displayedIssues.map((issue) => <li id={errorId(issue)} key={issue.key}><button type="button" className={styles.stayGuideIssueButton} onClick={() => focusIssue(issue)}>{issue.message}</button></li>)}</ol></section> : null}
        <label className={styles.stayGuideField}><span>页面标题</span><input {...fieldAccessibility("title")} aria-label="页面标题" value={draft.title} onChange={(event) => updateDraft({ ...draft, title: event.target.value })} /><small>必填，最多 80 字</small></label>
        <label className={styles.stayGuideField}><span>简短介绍</span><textarea {...fieldAccessibility("intro")} aria-label="简短介绍" value={draft.intro} onChange={(event) => updateDraft({ ...draft, intro: event.target.value })} /><small>必填，最多 240 字；提醒住客出发前复核现场信息</small></label>
        <ol {...fieldAccessibility("recommendations")} className={styles.stayGuideFaqList} tabIndex={-1}>
          {draft.recommendations.map((item, index) => <li key={item.id} className={styles.stayGuideSection}>
            <fieldset><legend>第 {index + 1} 条推荐</legend>
              <label className={styles.stayGuideField}><span>名称</span><input {...fieldAccessibility(recommendationTarget(index, "name"))} aria-label={`第 ${index + 1} 条推荐名称`} value={item.name} onChange={(event) => changeItem(index, { name: event.target.value })} /></label>
              <label className={styles.stayGuideField}><span>分类</span><select {...fieldAccessibility(recommendationTarget(index, "category"))} aria-label={`第 ${index + 1} 条推荐分类`} value={item.category} onChange={(event) => changeItem(index, { category: event.target.value as Category })}>{TRAVEL_CATEGORY_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
              <label className={styles.stayGuideField}><span>推荐理由</span><textarea {...fieldAccessibility(recommendationTarget(index, "reason"))} aria-label={`第 ${index + 1} 条推荐理由`} value={item.reason} onChange={(event) => changeItem(index, { reason: event.target.value })} /></label>
              <label className={styles.stayGuideField}><span>大概位置（选填）</span><input {...fieldAccessibility(recommendationTarget(index, "approximateLocation"))} aria-label={`第 ${index + 1} 条推荐大概位置`} value={item.approximateLocation || ""} onChange={(event) => changeItem(index, { approximateLocation: event.target.value || undefined })} /></label>
              <label className={styles.stayGuideField}><span>建议时长（选填）</span><textarea {...fieldAccessibility(recommendationTarget(index, "suggestedDuration"))} aria-label={`第 ${index + 1} 条推荐建议时长`} value={item.suggestedDuration || ""} onChange={(event) => changeItem(index, { suggestedDuration: event.target.value || undefined })} /></label>
              <label className={styles.stayGuideField}><span>适合人群</span><textarea {...fieldAccessibility(recommendationTarget(index, "audiences"))} aria-label={`第 ${index + 1} 条推荐适合人群`} value={item.audiences.join("、")} onChange={(event) => changeItem(index, { audiences: parseAudiences(event.target.value) })} /><small>用逗号、顿号或换行分隔，最多 6 类</small></label>
              <div {...fieldAccessibility(recommendationTarget(index, "image"))} className={styles.stayGuideImageField} tabIndex={-1}><strong>图片（选填）</strong>{item.image ? <><span>已保留经过媒体审核的公开图片</span><button type="button" aria-label={`移除第 ${index + 1} 条推荐图片`} onClick={() => changeItem(index, { image: undefined })}>移除图片</button></> : <span>本页暂不提供新图片上传；保存不会改动其他内容的图片。</span>}</div>
              <label className={styles.stayGuideToggle}><span>是否显示</span><input {...fieldAccessibility(recommendationTarget(index, "visible"))} type="checkbox" aria-label={`在小程序显示第 ${index + 1} 条推荐`} checked={item.visible} onChange={(event) => changeItem(index, { visible: event.target.checked })} />在小程序中显示</label>
              <div className={styles.stayGuideFaqActions}><button type="button" disabled={index === 0} aria-label={`上移第 ${index + 1} 条推荐`} onClick={() => moveItem(index, -1)}>上移</button><button type="button" disabled={index === draft.recommendations.length - 1} aria-label={`下移第 ${index + 1} 条推荐`} onClick={() => moveItem(index, 1)}>下移</button><button type="button" disabled={draft.recommendations.length === 1} aria-label={`删除第 ${index + 1} 条推荐`} onClick={() => updateDraft({ ...draft, recommendations: draft.recommendations.filter((_, itemIndex) => itemIndex !== index) })}>删除</button></div>
            </fieldset>
          </li>)}
        </ol>
        <button type="button" className={styles.editorSecondaryAction} disabled={draft.recommendations.length >= 50} onClick={() => updateDraft({ ...draft, recommendations: [...draft.recommendations, { id: uniqueId(draft.recommendations), name: "", category: "must_see", reason: "", audiences: ["所有住客"], visible: true }] })}>新增推荐</button>
      </div>}
      renderPreview={(width: ContentPreviewWidth) => <div><MobileTravelGuidePreview draft={draft} width={width} />{lastPreviewRevision !== null ? <p className={styles.durableNote}>服务器已检查当前内容版本 {lastPreviewRevision}</p> : null}</div>}
      technicalDetails={<><p>草稿内容版本：{revision}</p><p>线上发布版本：{publishedVersion || "尚未发布"}</p>{activeJobId ? <p>当前发布任务：{activeJobId}</p> : null}</>}
    />
    {conflictOpen && serverConflict ? <div className={styles.overlay}><div ref={conflictDialogRef} className={styles.confirmationDialog} role="dialog" aria-modal="true" aria-label="解决内容冲突" tabIndex={-1}><h2>解决内容冲突</h2><p>服务器标题：{serverConflict.draft.title}</p><p>本地标题：{draft.title}</p><div className={styles.dialogActions}><button ref={conflictPrimaryRef} type="button" className={styles.editorSecondaryAction} disabled={operation === "save"} onClick={useServerDraft}>使用服务器版本</button><button type="button" className={styles.editorPrimaryAction} disabled={operation === "save"} onClick={() => void runExclusive("save", () => performSave(serverConflict.revision, draftRef.current))}>以本地内容重新保存</button></div></div></div> : null}
    <PublishDialog open={publishOpen} job={publishJob} onClose={() => setPublishOpen(false)} trackingStatus={publishTrackingStatus} trackingError={publishTrackingError} onRetryTracking={onRetryPublishTracking} onRetry={() => void requestPublish()} contentTitle="灵山湾旅游攻略" isAdmin={role === "admin"} />
    <ReleaseHistoryDrawer open={historyOpen} releases={releases} canRollback={role === "admin"} onClose={closeHistory} status={releasesStatus} onRetry={onRetryReleases} actionsDisabled={operation !== "history" || jobInProgress} onRollback={requestRollback} />
  </>;
}
