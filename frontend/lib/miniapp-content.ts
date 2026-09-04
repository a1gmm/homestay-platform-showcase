import type { OwnerCaseDraft, OwnerDraft, OwnerDraftVideo } from "@/features/content/owner/types";

export interface OwnerDraftDiff {
  caseId: string;
  field: string;
  server: string;
  local: string;
}

export type OwnerPublishIssueField = "directory" | "title" | "description" | "primary" | "privacy" | "details";

export interface OwnerPublishIssue {
  message: string;
  caseIndex?: number;
  field: OwnerPublishIssueField;
}

export type PublishStageLabel =
  | "排队"
  | "校验"
  | "媒体发布"
  | "指针切换"
  | "线上验证"
  | "成功"
  | "失败"
  | "已回滚";

const fieldText = (value: string | null | undefined) => value?.trim() || "未填写";

function normalizedCase(item: OwnerCaseDraft): OwnerCaseDraft {
  return {
    caseId: item.caseId || undefined,
    title: item.title.trim(),
    description: item.description?.trim() || null,
    isVisible: item.isVisible,
    primaryMediaId: item.primaryMediaId || null,
    detailImages: item.detailImages.filter((image) => (
      image.alt.trim() && image.thumbnailMediaId && image.displayMediaId
    )).map((image) => ({ ...image, alt: image.alt.trim() })),
    privacyConfirmed: item.privacyConfirmed,
  };
}

export function buildOwnerDraftPayload(
  cases: OwnerCaseDraft[],
  revision: number,
  video?: OwnerDraftVideo | null,
): OwnerDraft {
  return {
    revision,
    cases: cases.map(normalizedCase),
    ...(video ? { video: { ...video, alt: video.alt.trim() } } : {}),
  };
}

export function moveOwnerCase<T extends OwnerCaseDraft>(
  cases: T[],
  fromIndex: number,
  toIndex: number,
): T[] {
  if (
    fromIndex === toIndex
    || fromIndex < 0
    || toIndex < 0
    || fromIndex >= cases.length
    || toIndex >= cases.length
  ) {
    return [...cases];
  }
  const next = [...cases];
  const [moved] = next.splice(fromIndex, 1);
  next.splice(toIndex, 0, moved);
  return next;
}

export function replaceOwnerCaseMedia(
  ownerCase: OwnerCaseDraft,
  slot: "primary",
  mediaId: string,
): OwnerCaseDraft {
  return { ...ownerCase, primaryMediaId: mediaId, privacyConfirmed: false };
}

export interface PublishFailurePresentation {
  title: string;
  canRetry: boolean;
  operatorGuidance?: string;
  closeLabel?: string;
}

const manualRecoveryPresentation: PublishFailurePresentation = {
  title: "发布任务需要人工恢复",
  canRetry: false,
  operatorGuidance:
    "请联系管理员或技术支持核对线上状态并完成恢复；当前内容无需修改，也不要重新提交发布。",
  closeLabel: "关闭，联系管理员",
};

const publishFailurePresentations: Record<string, PublishFailurePresentation> = {
  public_verification_error: { title: "线上内容校验未通过", canRetry: false },
  content_validation_error: { title: "发布内容校验未通过", canRetry: false },
  worker_lost: { title: "发布工作进程中断", canRetry: false },
  public_smoke_timeout: { title: "线上验证超时", canRetry: true },
  OSSRequestTimeout: { title: "媒体存储请求超时", canRetry: true },
  legacy_recovery_metadata_missing: manualRecoveryPresentation,
  manual_recovery_required: manualRecoveryPresentation,
};

export function getPublishFailurePresentation(errorCode: string | null | undefined): PublishFailurePresentation {
  return publishFailurePresentations[errorCode || ""] || { title: "发布任务未完成", canRetry: false };
}

export function getOwnerPublishIssues(draft: OwnerDraft): OwnerPublishIssue[] {
  const visible = draft.cases.filter((item) => item.isVisible);
  const issues: OwnerPublishIssue[] = [];
  if (visible.length < 4) {
    issues.push({
      message: `至少需要 4 套可见房源，当前 ${visible.length} 套`,
      field: "directory",
    });
  }

  draft.cases.forEach((item, caseIndex) => {
    if (!item.isVisible) return;
    const label = `第 ${caseIndex + 1} 套房源`;
    if (!item.title.trim() || item.title.trim().length > 80) {
      issues.push({ message: `${label}需要填写房源展示名称（1–80 个字）`, caseIndex, field: "title" });
    }
    if (!item.description?.trim() || item.description.trim().length > 240) {
      issues.push({ message: `${label}需要填写我们为这套房做了什么（1–240 个字）`, caseIndex, field: "description" });
    }
    if (!item.primaryMediaId) issues.push({ message: `${label}还没有首图`, caseIndex, field: "primary" });
    if (!item.privacyConfirmed) issues.push({ message: `${label}尚未确认图片授权和隐私`, caseIndex, field: "privacy" });
  });
  // Derivatives belong to one logical image and never count as two images.
  const imageCount = visible.reduce(
    (count, item) => count + item.detailImages.length,
    0,
  );
  if (imageCount > 20) {
    issues.push({ message: `房源照片最多展示 20 张，当前 ${imageCount} 张`, field: "details" });
  }
  return issues;
}

export function getOwnerPublishBlockers(draft: OwnerDraft): string[] {
  return getOwnerPublishIssues(draft).map((issue) => issue.message);
}

function caseKey(item: OwnerCaseDraft, index: number) {
  return item.caseId || `new-${index}`;
}

export function diffOwnerDraft(serverDraft: OwnerDraft, localDraft: OwnerDraft): OwnerDraftDiff[] {
  const serverMap = new Map(serverDraft.cases.map((item, index) => [caseKey(item, index), { item, index }]));
  const localMap = new Map(localDraft.cases.map((item, index) => [caseKey(item, index), { item, index }]));
  const keys = [
    ...Array.from(localMap.keys()),
    ...Array.from(serverMap.keys()).filter((key) => !localMap.has(key)),
  ];
  const result: OwnerDraftDiff[] = [];

  if (JSON.stringify(serverDraft.video || null) !== JSON.stringify(localDraft.video || null)) {
    result.push({
      caseId: "owner-video",
      field: "工作过程视频",
      server: serverDraft.video ? `${serverDraft.video.mediaId} / ${serverDraft.video.posterMediaId}` : "无",
      local: localDraft.video ? `${localDraft.video.mediaId} / ${localDraft.video.posterMediaId}` : "无",
    });
  }

  for (const key of keys) {
    const server = serverMap.get(key);
    const local = localMap.get(key);
    if (!server || !local) {
      result.push({
        caseId: key,
        field: "案例",
        server: server ? "存在" : "不存在",
        local: local ? "存在" : "不存在",
      });
      continue;
    }
    if (server.index !== local.index) {
      result.push({ caseId: key, field: "排序", server: `第 ${server.index + 1} 位`, local: `第 ${local.index + 1} 位` });
    }
    if (server.item.isVisible !== local.item.isVisible) {
      result.push({ caseId: key, field: "可见性", server: server.item.isVisible ? "显示" : "隐藏", local: local.item.isVisible ? "显示" : "隐藏" });
    }
    if (server.item.title.trim() !== local.item.title.trim()) {
      result.push({ caseId: key, field: "标题", server: fieldText(server.item.title), local: fieldText(local.item.title) });
    }
    if ((server.item.description?.trim() || "") !== (local.item.description?.trim() || "")) {
      result.push({ caseId: key, field: "说明", server: fieldText(server.item.description), local: fieldText(local.item.description) });
    }
    if (server.item.primaryMediaId !== local.item.primaryMediaId) {
      result.push({ caseId: key, field: "主图", server: fieldText(server.item.primaryMediaId), local: fieldText(local.item.primaryMediaId) });
    }
    if (JSON.stringify(server.item.detailImages) !== JSON.stringify(local.item.detailImages)) {
      result.push({ caseId: key, field: "详情图", server: server.item.detailImages.map((image) => image.alt).join("、") || "无", local: local.item.detailImages.map((image) => image.alt).join("、") || "无" });
    }
    if (server.item.privacyConfirmed !== local.item.privacyConfirmed) {
      result.push({ caseId: key, field: "素材授权", server: server.item.privacyConfirmed ? "已确认" : "未确认", local: local.item.privacyConfirmed ? "已确认" : "未确认" });
    }
  }
  return result;
}

export function mapPublishStage(job: { status: string; progressStage: string | null }): PublishStageLabel {
  if (job.status === "failed") return "失败";
  if (job.status === "rolled_back") return "已回滚";
  if (job.status === "succeeded") return "成功";
  const stage = job.progressStage || "queued";
  if (stage === "validating" || stage === "validating_rollback") return "校验";
  if (["promoting_media", "manifest_intent", "uploading_manifest"].includes(stage)) return "媒体发布";
  if (["verifying_candidate", "swapping_pointer"].includes(stage)) return "指针切换";
  if (["observing_pointer", "finalizing"].includes(stage)) return "线上验证";
  return "排队";
}
