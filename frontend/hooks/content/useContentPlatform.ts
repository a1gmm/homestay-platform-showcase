"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { miniappContentApi } from "@/features/content/api";
import type { ContentChannel, ContentChannelUpdate } from "@/features/content/types";

function createChannelQueryKeys(channel: ContentChannel) {
  return {
    channel: ["miniapp-content", channel, "channel"] as const,
    dashboard: ["miniapp-content", channel, "dashboard"] as const,
    draft: ["miniapp-content", channel, "draft"] as const,
    releases: ["miniapp-content", channel, "releases"] as const,
    publishJob: (jobId: string) => ["miniapp-content", channel, `publish-job:${jobId}`] as const,
  };
}

const ownerQueryKeys = createChannelQueryKeys("owner");

export const contentQueryKeys = {
  forChannel: createChannelQueryKeys,
  channel: (channel: ContentChannel) => createChannelQueryKeys(channel).channel,
  // Owner aliases preserve the established owner hooks while new channels use the factory.
  dashboard: ownerQueryKeys.dashboard,
  draft: ownerQueryKeys.draft,
  releases: ownerQueryKeys.releases,
  publishJob: ownerQueryKeys.publishJob,
};

interface PersistentContentJobState {
  storageKey: string | null;
  jobId: string | null;
}

export function usePersistentContentJob(
  channel: ContentChannel,
  userId: string | null | undefined,
) {
  const storageKey = useMemo(
    () => userId ? `miniapp-content:${channel}:active-job:${userId}` : null,
    [channel, userId],
  );
  const [jobState, setJobState] = useState<PersistentContentJobState>({
    storageKey: null,
    jobId: null,
  });

  useEffect(() => {
    try {
      setJobState({
        storageKey,
        jobId: storageKey ? window.sessionStorage.getItem(storageKey) : null,
      });
    } catch {
      setJobState({ storageKey, jobId: null });
    }
  }, [storageKey]);

  const trackJob = useCallback((jobId: string) => {
    setJobState({ storageKey, jobId });
    try {
      if (storageKey) window.sessionStorage.setItem(storageKey, jobId);
    } catch {
      // In-memory polling remains available when session storage is unavailable.
    }
  }, [storageKey]);

  const clearPersistedJob = useCallback((jobId: string) => {
    try {
      if (storageKey && window.sessionStorage.getItem(storageKey) === jobId) {
        window.sessionStorage.removeItem(storageKey);
      }
    } catch {
      // The mounted screen can still display the terminal job without storage.
    }
    setJobState((current) => current.storageKey === storageKey && current.jobId === jobId
      ? { ...current, jobId: null }
      : current);
  }, [storageKey]);

  return {
    activeJobId: jobState.storageKey === storageKey ? jobState.jobId : null,
    trackJob,
    clearPersistedJob,
  };
}

export function useContentDashboard() {
  return useQuery({
    queryKey: contentQueryKeys.dashboard,
    queryFn: () => miniappContentApi.getDashboard(),
  });
}

export function useUpdateContentChannel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ContentChannelUpdate) => miniappContentApi.updateChannel(input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: contentQueryKeys.dashboard });
    },
  });
}
