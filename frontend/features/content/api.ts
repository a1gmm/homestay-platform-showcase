import axios, { type AxiosProgressEvent } from "axios";
import { api } from "@/lib/api";
import type {
  ContentChannel,
  ContentReleaseHistory,
  ContentChannelUpdate,
  ContentDashboard,
  OwnerRelease,
  PublishStructuredDraftInput,
  PublishJob,
  PublishJobAccepted,
  RollbackStructuredReleaseInput,
  SaveStructuredDraftInput,
  StructuredContentChannel,
  StructuredDraftRevision,
  StructuredGuidePreview,
} from "./types";
import type {
  OwnerDraft,
  OwnerDraftPreview,
  OwnerContentMedia,
  OwnerImageUploadPair,
  OwnerMediaPreviewUrl,
  PublishOwnerInput,
  RollbackOwnerInput,
  SaveOwnerDraftInput,
  OwnerVideoUploadInput,
  OwnerVideoUploadTicket,
} from "./owner/types";

const CONTENT_ROOT = "/miniapp-content";

/** Content-platform requests remain feature-local so the legacy global API stays untouched. */
export const miniappContentApi = {
  async getDashboard(): Promise<ContentDashboard> {
    return (await api.get<ContentDashboard>(`${CONTENT_ROOT}/dashboard`)).data;
  },
  async updateChannel(input: ContentChannelUpdate): Promise<ContentChannelUpdate> {
    return (await api.patch<ContentChannelUpdate>(`${CONTENT_ROOT}/channels/${input.channel}`, {
      enabled: input.enabled,
    })).data;
  },
  async getOwnerDraft(): Promise<OwnerDraft> {
    return (await api.get<OwnerDraft>(`${CONTENT_ROOT}/owner/draft`)).data;
  },
  async saveOwnerDraft(input: SaveOwnerDraftInput): Promise<OwnerDraft> {
    return (await api.put<OwnerDraft>(`${CONTENT_ROOT}/owner/draft`, input)).data;
  },
  async getStructuredDraft<C extends StructuredContentChannel>(
    channel: C,
  ): Promise<StructuredDraftRevision<C>> {
    return (await api.get<StructuredDraftRevision<C>>(`${CONTENT_ROOT}/${channel}/draft`)).data;
  },
  async saveStructuredDraft<C extends StructuredContentChannel>(
    channel: C,
    input: SaveStructuredDraftInput<C>,
  ): Promise<StructuredDraftRevision<C>> {
    return (await api.put<StructuredDraftRevision<C>>(`${CONTENT_ROOT}/${channel}/draft`, input)).data;
  },
  async previewStructuredDraft<C extends StructuredContentChannel>(
    channel: C,
    revision: number,
  ): Promise<StructuredGuidePreview<C>> {
    return (await api.post<StructuredGuidePreview<C>>(`${CONTENT_ROOT}/${channel}/preview`, { revision })).data;
  },
  async previewOwnerDraft(revision: number): Promise<OwnerDraftPreview> {
    return (await api.post<OwnerDraftPreview>(`${CONTENT_ROOT}/owner/preview`, { revision })).data;
  },
  async uploadOwnerImage(
    file: File,
    onProgress: (percent: number) => void,
    signal?: AbortSignal,
  ): Promise<OwnerContentMedia> {
    const body = new FormData();
    body.append("file", file);
    return (await api.post<OwnerContentMedia>(`${CONTENT_ROOT}/media`, body, {
      signal,
      onUploadProgress: (event: AxiosProgressEvent) => {
        if (event.total) onProgress((event.loaded / event.total) * 100);
      },
    })).data;
  },
  async uploadOwnerImagePair(
    file: File,
    onProgress: (percent: number) => void,
    signal?: AbortSignal,
  ): Promise<OwnerImageUploadPair> {
    const body = new FormData();
    body.append("file", file);
    return (await api.post<OwnerImageUploadPair>(`${CONTENT_ROOT}/media/image-pair`, body, {
      signal,
      onUploadProgress: (event: AxiosProgressEvent) => {
        if (event.total) onProgress((event.loaded / event.total) * 100);
      },
    })).data;
  },
  async createOwnerVideoUpload(input: OwnerVideoUploadInput, signal?: AbortSignal): Promise<OwnerVideoUploadTicket> {
    return (await api.post<OwnerVideoUploadTicket>(`${CONTENT_ROOT}/media/video-upload`, input, { signal })).data;
  },
  async putOwnerVideo(
    ticket: OwnerVideoUploadTicket,
    file: File,
    onProgress: (percent: number) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    await axios.put(ticket.uploadUrl, file, {
      headers: ticket.headers,
      withCredentials: false,
      signal,
      timeout: 120_000,
      onUploadProgress: (event: AxiosProgressEvent) => {
        if (event.total) onProgress((event.loaded / event.total) * 100);
      },
    });
  },
  async finalizeOwnerMedia(mediaId: string, signal?: AbortSignal): Promise<OwnerContentMedia> {
    return (await api.post<OwnerContentMedia>(`${CONTENT_ROOT}/media/${mediaId}/finalize`, undefined, { signal })).data;
  },
  async getOwnerMediaPreviewUrl(mediaId: string): Promise<OwnerMediaPreviewUrl> {
    return (await api.get<OwnerMediaPreviewUrl>(`${CONTENT_ROOT}/media/${mediaId}/preview-url`)).data;
  },
  async publishOwner(input: PublishOwnerInput): Promise<PublishJobAccepted> {
    return (await api.post<PublishJobAccepted>(`${CONTENT_ROOT}/owner/publish`, {
      revision: input.revision,
    }, {
      headers: { "Idempotency-Key": input.idempotencyKey },
    })).data;
  },
  async publishStructuredDraft<C extends StructuredContentChannel>(
    channel: C,
    input: PublishStructuredDraftInput,
  ): Promise<PublishJobAccepted> {
    return (await api.post<PublishJobAccepted>(`${CONTENT_ROOT}/${channel}/publish`, {
      revision: input.revision,
    }, {
      headers: { "Idempotency-Key": input.idempotencyKey },
    })).data;
  },
  async getPublishJob(jobId: string): Promise<PublishJob> {
    return (await api.get<PublishJob>(`${CONTENT_ROOT}/publish-jobs/${jobId}`)).data;
  },
  async getOwnerReleases(): Promise<{ releases: OwnerRelease[] }> {
    return (await api.get<{ releases: OwnerRelease[] }>(`${CONTENT_ROOT}/owner/releases`)).data;
  },
  async getStructuredReleases<C extends StructuredContentChannel>(
    channel: C,
  ): Promise<ContentReleaseHistory> {
    return (await api.get<ContentReleaseHistory>(`${CONTENT_ROOT}/${channel}/releases`)).data;
  },
  async rollbackOwner(input: RollbackOwnerInput): Promise<PublishJobAccepted> {
    return (await api.post<PublishJobAccepted>(`${CONTENT_ROOT}/owner/releases/${input.version}/rollback`, {}, {
      headers: { "Idempotency-Key": input.idempotencyKey },
    })).data;
  },
  async rollbackStructuredRelease<C extends StructuredContentChannel>(
    channel: C,
    input: RollbackStructuredReleaseInput,
  ): Promise<PublishJobAccepted> {
    return (await api.post<PublishJobAccepted>(
      `${CONTENT_ROOT}/${channel}/releases/${encodeURIComponent(input.version)}/rollback`,
      {},
      { headers: { "Idempotency-Key": input.idempotencyKey } },
    )).data;
  },
};

export type { ContentChannel };
