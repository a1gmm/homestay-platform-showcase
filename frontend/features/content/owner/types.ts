import type { OwnerManifest } from "../../../../miniapp/content-contract/types";

export type { OwnerManifest } from "../../../../miniapp/content-contract/types";

export interface OwnerImageDraft {
  alt: string;
  thumbnailMediaId: string;
  displayMediaId: string;
}

export interface OwnerCaseDraft {
  caseId?: string;
  title: string;
  description: string | null;
  isVisible: boolean;
  primaryMediaId: string | null;
  detailImages: OwnerImageDraft[];
  privacyConfirmed: boolean;
}

export interface OwnerDraftVideo {
  mediaId: string;
  posterMediaId: string;
  alt: string;
}

export interface OwnerDraft {
  revision: number;
  cases: OwnerCaseDraft[];
  video?: OwnerDraftVideo | null;
}

export interface SaveOwnerDraftInput extends OwnerDraft {}

export interface PublishOwnerInput {
  revision: number;
  idempotencyKey: string;
}

export interface RollbackOwnerInput {
  version: string;
  idempotencyKey: string;
}

export interface OwnerContentMedia {
  mediaId: string;
  mediaType: "image" | "video";
  mimeType: string;
  sizeBytes: number;
  sha256: string;
}

export interface OwnerImageDerivativeMedia extends OwnerContentMedia {
  mediaType: "image";
  derivativeRole: "thumbnail" | "display";
}

export interface OwnerImageUploadPair {
  derivativeSetId: string;
  sourceSha256: string;
  thumbnail: OwnerImageDerivativeMedia & { derivativeRole: "thumbnail" };
  display: OwnerImageDerivativeMedia & { derivativeRole: "display" };
}

export interface OwnerVideoUploadInput {
  originalName: string;
  byteSize: number;
  sha256: string;
}

export interface OwnerVideoUploadTicket {
  mediaId: string;
  uploadUrl: string;
  expiresSeconds: number;
  method: "PUT";
  headers: Record<string, string>;
}

export interface OwnerMediaPreviewUrl {
  url: string;
  expiresSeconds: number;
}

// 空草稿预览只会返回 { cases: [] }；非空预览遵循生成的公开契约。
export type OwnerDraftPreview = OwnerManifest | { cases: [] };
