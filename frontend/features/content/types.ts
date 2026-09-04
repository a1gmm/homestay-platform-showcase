import type {
  StayGuideDraft,
  StayGuideManifest,
  TravelGuideDraft,
  TravelGuideManifest,
} from "../../../miniapp/content-contract/guides";

export type {
  StayGuideDraft,
  StayGuideManifest,
  TravelGuideDraft,
  TravelGuideManifest,
} from "../../../miniapp/content-contract/guides";

export type ContentChannel = "owner" | "stay_guide" | "travel";
export type StructuredContentChannel = Exclude<ContentChannel, "owner">;

export interface ContentValidationIssue {
  path: string;
  reason: string;
}

export type StructuredDraftFor<C extends StructuredContentChannel> =
  C extends "stay_guide" ? StayGuideDraft : TravelGuideDraft;

export type StructuredPreviewManifestFor<C extends StructuredContentChannel> =
  C extends "stay_guide"
    ? Omit<StayGuideManifest, "schema"> & { schema: "guanhaiju.stay_guide.preview.v1" }
    : Omit<TravelGuideManifest, "schema"> & { schema: "guanhaiju.travel.preview.v1" };

export interface StructuredDraftRevision<C extends StructuredContentChannel> {
  channel: C;
  revision: number;
  draft: StructuredDraftFor<C>;
  draftDirty: boolean;
  savedAt: string | null;
  savedBy: string | null;
}

export interface SaveStructuredDraftInput<C extends StructuredContentChannel> {
  expectedRevision: number;
  draft: StructuredDraftFor<C>;
}

export interface StructuredGuidePreview<C extends StructuredContentChannel> {
  channel: C;
  revision: number;
  manifest: StructuredPreviewManifestFor<C>;
}

export interface PublishStructuredDraftInput {
  revision: number;
  idempotencyKey: string;
}

export interface RollbackStructuredReleaseInput {
  version: string;
  idempotencyKey: string;
}

export interface ContentErrorEnvelope {
  code: string;
  message: string;
  trace_id: string;
  retryable: boolean;
  issues?: ContentValidationIssue[];
}

export interface ContentDashboardChannel {
  channel: ContentChannel;
  enabled: boolean;
  revision: number;
  draftDirty: boolean;
  publishedVersion: string | null;
}

export interface ContentDashboard {
  channels: ContentDashboardChannel[];
}

export interface ContentChannelUpdate {
  channel: ContentChannel;
  enabled: boolean;
}

export interface PublishJob {
  jobId: string;
  channel: ContentChannel;
  operation: "publish" | "rollback";
  status: string;
  expectedRevision: number | null;
  targetVersion: string | null;
  progressStage: string | null;
  progressPercent: number | null;
  errorCode: string | null;
}

export interface PublishJobAccepted {
  jobId: string;
  channel: ContentChannel;
  status: string;
  expectedRevision?: number;
  targetVersion?: string | null;
}

export interface ContentRelease {
  version: string;
  status: string;
  publishedAt: string | null;
}

/** The established owner endpoint only returns completed releases. */
export interface OwnerRelease extends Omit<ContentRelease, "publishedAt"> {
  publishedAt: string;
}

export interface ContentReleaseHistory {
  releases: ContentRelease[];
}

interface ContentHttpError {
  response?: {
    status?: number;
    data?: Partial<ContentErrorEnvelope> & { detail?: unknown };
  };
}

export class ContentClientError extends Error {
  readonly status: number | null;
  readonly code: string | null;
  readonly retryable: boolean;
  readonly traceId: string | null;
  readonly issues: ContentValidationIssue[];

  constructor(
    message: string,
    options: {
      status?: number | null;
      code?: string | null;
      retryable?: boolean;
      traceId?: string | null;
      issues?: ContentValidationIssue[];
    } = {},
  ) {
    super(message);
    this.name = "ContentClientError";
    this.status = options.status ?? null;
    this.code = options.code ?? null;
    this.retryable = options.retryable ?? false;
    this.traceId = options.traceId ?? null;
    this.issues = options.issues ?? [];
  }
}

/** Convert API failures into actionable copy without exposing backend details. */
export function toContentClientError(
  error: unknown,
  fallback: string,
  forbiddenMessage = "当前账号无权进行此操作，请联系管理员处理。",
): ContentClientError {
  if (error instanceof ContentClientError) return error;

  const httpError = error as ContentHttpError;
  const status = httpError?.response?.status ?? null;
  const envelope = httpError?.response?.data;
  const code = typeof envelope?.code === "string" ? envelope.code : null;
  const retryable = envelope?.retryable === true;
  const traceId = typeof envelope?.trace_id === "string" ? envelope.trace_id : null;
  const issues = Array.isArray(envelope?.issues)
    ? envelope.issues.slice(0, 100).filter((issue): issue is ContentValidationIssue =>
      !!issue
      && typeof issue === "object"
      && typeof issue.path === "string"
      && typeof issue.reason === "string")
    : [];

  if (!httpError?.response) {
    return new ContentClientError("网络异常，请检查网络后重试。", { retryable: true });
  }
  if (status === 401) {
    return new ContentClientError("登录已过期，请重新登录。", { status, code, retryable, traceId });
  }
  if (status === 403) {
    return new ContentClientError(forbiddenMessage, {
      status,
      code,
      retryable: false,
      traceId,
    });
  }
  if (status === 409 && code === "draft_conflict") {
    return new ContentClientError("内容已被其他人修改，请刷新后再保存。", {
      status,
      code,
      retryable: false,
      traceId,
    });
  }
  if (status === 409) {
    return new ContentClientError("该栏目正在处理其他操作，请稍后重试。", {
      status,
      code,
      retryable,
      traceId,
    });
  }
  if (status === 422) {
    return new ContentClientError("内容未通过检查，请按页面提示修改后重试。", {
      status,
      code,
      retryable: false,
      traceId,
      issues,
    });
  }
  if (status !== null && status >= 500) {
    return new ContentClientError("服务暂时不可用，请稍后重试。", {
      status,
      code,
      retryable: true,
      traceId,
    });
  }
  return new ContentClientError(fallback, { status, code, retryable, traceId });
}

export function isContentConflict(error: unknown): error is {
  response: { status: number; data: ContentErrorEnvelope };
} {
  if (!error || typeof error !== "object" || !("response" in error)) return false;
  const response = (error as { response?: unknown }).response;
  return !!response
    && typeof response === "object"
    && "status" in response
    && (response as { status?: unknown }).status === 409;
}
