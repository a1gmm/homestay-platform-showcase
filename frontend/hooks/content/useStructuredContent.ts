"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { miniappContentApi } from "@/features/content/api";
import {
  ContentClientError,
  toContentClientError,
  type PublishJobAccepted,
  type PublishStructuredDraftInput,
  type RollbackStructuredReleaseInput,
  type SaveStructuredDraftInput,
  type StructuredContentChannel,
  type StructuredDraftRevision,
} from "@/features/content/types";
import { contentQueryKeys, usePersistentContentJob } from "./useContentPlatform";

const TERMINAL_JOB_STATUSES = new Set(["succeeded", "failed", "rolled_back"]);

export function isStructuredContentTerminalJobStatus(status: string): boolean {
  return TERMINAL_JOB_STATUSES.has(status.toLowerCase());
}

export interface StructuredDraftConflict<C extends StructuredContentChannel> {
  error: ContentClientError;
  localInput: SaveStructuredDraftInput<C>;
  cachedDraft: StructuredDraftRevision<C> | undefined;
}

function mergeStructuredDraft<C extends StructuredContentChannel>(
  current: StructuredDraftRevision<C> | undefined,
  incoming: StructuredDraftRevision<C>,
  acceptEqualRevision = false,
): StructuredDraftRevision<C> {
  if (!current || incoming.revision > current.revision) return incoming;
  if (acceptEqualRevision && incoming.revision === current.revision) return incoming;
  return current;
}

function setAuthoritativeStructuredDraft<C extends StructuredContentChannel>(
  queryClient: ReturnType<typeof useQueryClient>,
  queryKey: ReturnType<typeof contentQueryKeys.forChannel>["draft"],
  incoming: StructuredDraftRevision<C>,
): StructuredDraftRevision<C> {
  const current = queryClient.getQueryData<StructuredDraftRevision<C>>(queryKey);
  const accepted = mergeStructuredDraft(current, incoming, true);

  if (accepted === current) return current;

  const query = queryClient.getQueryCache().find({ queryKey, exact: true });
  if (query) {
    query.setState({
      data: accepted,
      dataUpdatedAt: Date.now(),
      error: null,
      isInvalidated: false,
      status: "success",
    });
    return accepted;
  }

  queryClient.setQueryData<StructuredDraftRevision<C>>(queryKey, accepted);
  return accepted;
}

async function contentAction<T>(
  action: string,
  operation: () => Promise<T>,
  forbiddenMessage?: string,
): Promise<T> {
  try {
    return await operation();
  } catch (error) {
    throw toContentClientError(error, `${action}失败，请稍后重试。`, forbiddenMessage);
  }
}

export function useStructuredContent<C extends StructuredContentChannel>(
  channel: C,
  userId?: string | null,
) {
  const queryClient = useQueryClient();
  const keys = useMemo(() => contentQueryKeys.forChannel(channel), [channel]);
  const { activeJobId, trackJob, clearPersistedJob } = usePersistentContentJob(channel, userId);
  const [conflict, setConflict] = useState<StructuredDraftConflict<C> | null>(null);

  const invalidateDashboard = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: contentQueryKeys.dashboard });
  }, [queryClient]);

  const refreshDraft = useCallback(async () => {
    const serverDraft = await contentAction(
      "刷新内容",
      () => miniappContentApi.getStructuredDraft(channel),
    );
    return setAuthoritativeStructuredDraft(queryClient, keys.draft, serverDraft);
  }, [channel, keys.draft, queryClient]);

  const invalidatePublishedContent = useCallback(() => {
    invalidateDashboard();
    void refreshDraft().catch(() => undefined);
    void queryClient.invalidateQueries({ queryKey: keys.releases });
  }, [invalidateDashboard, keys.releases, queryClient, refreshDraft]);

  const draft = useQuery<StructuredDraftRevision<C>, ContentClientError>({
    queryKey: keys.draft,
    queryFn: () => contentAction("读取内容", () => miniappContentApi.getStructuredDraft(channel)),
    structuralSharing: (current, incoming) => mergeStructuredDraft(
      current as StructuredDraftRevision<C> | undefined,
      incoming as StructuredDraftRevision<C>,
    ),
  });

  const save = useMutation<
    StructuredDraftRevision<C>,
    ContentClientError,
    SaveStructuredDraftInput<C>,
    { localInput: SaveStructuredDraftInput<C>; cachedDraft: StructuredDraftRevision<C> | undefined }
  >({
    mutationFn: (input) => contentAction(
      "保存内容",
      () => miniappContentApi.saveStructuredDraft(channel, input),
    ),
    onMutate: async (localInput) => {
      await queryClient.cancelQueries({ queryKey: keys.draft });
      const cachedDraft = queryClient.getQueryData<StructuredDraftRevision<C>>(keys.draft);
      setConflict(null);
      return { localInput, cachedDraft };
    },
    onSuccess: (savedDraft) => {
      setConflict(null);
      setAuthoritativeStructuredDraft(queryClient, keys.draft, savedDraft);
      invalidateDashboard();
    },
    onError: (error, localInput, context) => {
      if (error.status === 409 && error.code === "draft_conflict") {
        setConflict({
          error,
          localInput: context?.localInput ?? localInput,
          cachedDraft: context?.cachedDraft,
        });
      }
    },
  });

  const preview = useMutation({
    mutationFn: (revision: number) => contentAction(
      "生成预览",
      () => miniappContentApi.previewStructuredDraft(channel, revision),
    ),
  });

  const startPolling = useCallback((accepted: PublishJobAccepted) => {
    trackJob(accepted.jobId);
    invalidateDashboard();
    void queryClient.invalidateQueries({ queryKey: keys.releases });
  }, [invalidateDashboard, keys.releases, queryClient, trackJob]);

  const publish = useMutation({
    mutationFn: (input: PublishStructuredDraftInput) => contentAction(
      "发布内容",
      () => miniappContentApi.publishStructuredDraft(channel, input),
      "内容已经准备好，请联系管理员发布。",
    ),
    onSuccess: startPolling,
  });

  const history = useQuery({
    queryKey: keys.releases,
    queryFn: () => contentAction(
      "读取历史版本",
      () => miniappContentApi.getStructuredReleases(channel),
    ),
  });

  const rollback = useMutation({
    mutationFn: (input: RollbackStructuredReleaseInput) => contentAction(
      "恢复历史版本",
      () => miniappContentApi.rollbackStructuredRelease(channel, input),
      "仅管理员可以恢复历史版本，请联系管理员处理。",
    ),
    onSuccess: startPolling,
  });

  const publishJob = useQuery({
    queryKey: keys.publishJob(activeJobId ?? "none"),
    queryFn: () => contentAction(
      "读取发布进度",
      () => miniappContentApi.getPublishJob(activeJobId as string),
    ),
    enabled: activeJobId !== null,
    refetchInterval: (query) => {
      const job = query.state.data;
      return job && isStructuredContentTerminalJobStatus(job.status) ? false : 2000;
    },
  });

  useEffect(() => {
    if (publishJob.data && isStructuredContentTerminalJobStatus(publishJob.data.status)) {
      invalidatePublishedContent();
      clearPersistedJob(publishJob.data.jobId);
    }
  }, [clearPersistedJob, invalidatePublishedContent, publishJob.data]);

  return {
    channel,
    draft,
    save: {
      ...save,
      conflict,
      clearConflict: () => setConflict(null),
    },
    preview,
    publish,
    publishJob,
    history,
    rollback,
    activeJobId,
    refreshDraft,
    resumePublishJob: trackJob,
  };
}
