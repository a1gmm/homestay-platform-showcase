"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { miniappContentApi } from "@/features/content/api";
import { isContentConflict } from "@/features/content/types";
import type {
  OwnerContentMedia,
  OwnerImageUploadPair,
  OwnerDraft,
  OwnerMediaPreviewUrl,
  PublishOwnerInput,
  RollbackOwnerInput,
  SaveOwnerDraftInput,
} from "@/features/content/owner/types";
import { contentQueryKeys, usePersistentContentJob } from "./useContentPlatform";

const TERMINAL_JOB_STATUSES = new Set(["succeeded", "failed", "rolled_back"]);

export interface OwnerDraftConflict {
  error: { code: string; message: string; trace_id: string; retryable: boolean };
  localDraft: SaveOwnerDraftInput;
  cachedDraft: OwnerDraft | undefined;
}

interface OwnerDraftMergeOptions {
  acceptEqualRevision?: boolean;
}

/**
 * Background reads can finish out of order.  A lower revision must never
 * replace an already-observed draft; equal revisions are retained unless the
 * caller has an explicit server-authoritative reason to replace them.
 */
export function mergeOwnerDraft(
  current: OwnerDraft | undefined,
  incoming: OwnerDraft,
  options: OwnerDraftMergeOptions = {},
): OwnerDraft {
  if (!current || incoming.revision > current.revision) {
    return incoming;
  }

  if (incoming.revision === current.revision && options.acceptEqualRevision) {
    return incoming;
  }

  return current;
}

function setAuthoritativeOwnerDraft(
  queryClient: ReturnType<typeof useQueryClient>,
  incoming: OwnerDraft,
): OwnerDraft {
  const current = queryClient.getQueryData<OwnerDraft>(contentQueryKeys.draft);
  const accepted = mergeOwnerDraft(current, incoming, { acceptEqualRevision: true });

  if (accepted === current) {
    return current;
  }

  const query = queryClient.getQueryCache().find({
    queryKey: contentQueryKeys.draft,
    exact: true,
  });
  if (query) {
    // setQueryData would reapply this query's background structuralSharing.
    query.setState({
      data: accepted,
      dataUpdatedAt: Date.now(),
      error: null,
      isInvalidated: false,
      status: "success",
    });
    return accepted;
  }

  queryClient.setQueryData<OwnerDraft>(contentQueryKeys.draft, accepted);
  return accepted;
}

function invalidatePublishedContent(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: contentQueryKeys.dashboard });
  queryClient.invalidateQueries({ queryKey: contentQueryKeys.releases });
}

export function useOwnerDraft() {
  const queryClient = useQueryClient();
  const query = useQuery<OwnerDraft>({
    queryKey: contentQueryKeys.draft,
    queryFn: () => miniappContentApi.getOwnerDraft(),
    structuralSharing: (current, incoming) => mergeOwnerDraft(
      current as OwnerDraft | undefined,
      incoming as OwnerDraft,
    ),
  });
  const reload = async () => {
    const serverDraft = await miniappContentApi.getOwnerDraft();
    return setAuthoritativeOwnerDraft(queryClient, serverDraft);
  };

  return { ...query, reload };
}

export function useSaveOwnerDraft() {
  const queryClient = useQueryClient();
  const [conflict, setConflict] = useState<OwnerDraftConflict | null>(null);
  const mutation = useMutation({
    mutationFn: (input: SaveOwnerDraftInput) => miniappContentApi.saveOwnerDraft(input),
    onMutate: async (localDraft) => {
      await queryClient.cancelQueries({ queryKey: contentQueryKeys.draft });
      const cachedDraft = queryClient.getQueryData<OwnerDraft>(contentQueryKeys.draft);
      setConflict(null);
      return { localDraft, cachedDraft };
    },
    onSuccess: async (savedDraft) => {
      // Never calculate a revision locally. The save response is the only authority.
      await queryClient.cancelQueries({ queryKey: contentQueryKeys.draft });
      setConflict(null);
      setAuthoritativeOwnerDraft(queryClient, savedDraft);
      queryClient.invalidateQueries({ queryKey: contentQueryKeys.dashboard });
    },
    onError: (error, localDraft, context) => {
      if (isContentConflict(error)) {
        setConflict({
          error: error.response.data,
          localDraft: context?.localDraft || localDraft,
          cachedDraft: context?.cachedDraft,
        });
      }
    },
  });
  return { ...mutation, conflict, clearConflict: () => setConflict(null) };
}

export function usePreviewOwnerDraft() {
  return useMutation({
    mutationFn: (revision: number) => miniappContentApi.previewOwnerDraft(revision),
  });
}

export function usePublishOwner() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: PublishOwnerInput) => miniappContentApi.publishOwner(input),
    onSuccess: () => invalidatePublishedContent(queryClient),
  });
}

export function useOwnerPublishJob(jobId: string | null) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: contentQueryKeys.publishJob(jobId || "none"),
    queryFn: () => miniappContentApi.getPublishJob(jobId as string),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const job = query.state.data;
      return job && TERMINAL_JOB_STATUSES.has(job.status) ? false : 2000;
    },
  });
  useEffect(() => {
    if (query.data && TERMINAL_JOB_STATUSES.has(query.data.status)) {
      invalidatePublishedContent(queryClient);
    }
  }, [query.data, queryClient]);
  return query;
}

export function useOwnerReleases() {
  return useQuery({
    queryKey: contentQueryKeys.releases,
    queryFn: () => miniappContentApi.getOwnerReleases(),
  });
}

export function useRollbackOwner() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: RollbackOwnerInput) => miniappContentApi.rollbackOwner(input),
    onSuccess: () => invalidatePublishedContent(queryClient),
  });
}

export function useUploadOwnerImage() {
  return useMutation({
    mutationFn: ({ file, onProgress, signal }: { file: File; onProgress: (percent: number) => void; signal?: AbortSignal }) =>
      miniappContentApi.uploadOwnerImage(file, onProgress, signal),
  });
}

export function useUploadOwnerImagePair() {
  return useMutation<
    OwnerImageUploadPair,
    Error,
    { file: File; onProgress: (percent: number) => void; signal?: AbortSignal }
  >({
    mutationFn: ({ file, onProgress, signal }) =>
      miniappContentApi.uploadOwnerImagePair(file, onProgress, signal),
  });
}

export function useUploadOwnerVideo() {
  return useMutation({
    mutationFn: async ({
      file,
      progress,
      sha256: suppliedSha256,
      signal,
    }: {
      file: File;
      progress: { transfer(percent: number): void; processing(label: string): void };
      sha256?: string;
      signal?: AbortSignal;
    }): Promise<OwnerContentMedia> => {
      const sha256 = suppliedSha256 || await sha256OwnerFile(file);
      const ticket = await miniappContentApi.createOwnerVideoUpload({
        originalName: file.name,
        byteSize: file.size,
        sha256,
      }, signal);
      await miniappContentApi.putOwnerVideo(ticket, file, progress.transfer, signal);
      progress.processing("服务端校验中");
      return miniappContentApi.finalizeOwnerMedia(ticket.mediaId, signal);
    },
  });
}

export async function sha256OwnerFile(file: File): Promise<string> {
  if (!globalThis.crypto?.subtle) {
    throw new Error("当前浏览器无法计算视频校验值，请升级浏览器后重试");
  }
  const digest = await globalThis.crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function useOwnerMediaPreviewUrl() {
  return useMutation<OwnerMediaPreviewUrl, Error, string>({
    mutationFn: (mediaId) => miniappContentApi.getOwnerMediaPreviewUrl(mediaId),
  });
}

export function usePersistentOwnerJob(userId: string | null | undefined) {
  return usePersistentContentJob("owner", userId);
}
