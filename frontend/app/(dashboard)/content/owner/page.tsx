"use client";

import { useEffect, useMemo } from "react";
import { Alert, Skeleton } from "antd";
import { useRouter } from "next/navigation";
import { OwnerCaseEditor } from "@/components/content/OwnerCaseEditor";
import ownerStyles from "@/components/content/owner-content.module.css";
import { useAuthStore } from "@/lib/auth";
import { useContentDashboard } from "@/hooks/content/useContentPlatform";
import {
  useOwnerDraft,
  useOwnerMediaPreviewUrl,
  usePersistentOwnerJob,
  useOwnerPublishJob,
  useOwnerReleases,
  usePreviewOwnerDraft,
  usePublishOwner,
  useRollbackOwner,
  useSaveOwnerDraft,
  useUploadOwnerImage,
  useUploadOwnerImagePair,
  useUploadOwnerVideo,
} from "@/hooks/content/useOwnerContent";

export default function OwnerContentPage() {
  const user = useAuthStore((state) => state.user);
  const role = user?.role;
  const router = useRouter();
  const { activeJobId, trackJob, clearPersistedJob } = usePersistentOwnerJob(user?.user_id);
  const dashboard = useContentDashboard();
  const ownerDraft = useOwnerDraft();
  const saveDraft = useSaveOwnerDraft();
  const previewDraft = usePreviewOwnerDraft();
  const publishOwner = usePublishOwner();
  const publishJob = useOwnerPublishJob(activeJobId);
  const releases = useOwnerReleases();
  const rollbackOwner = useRollbackOwner();
  const uploadImage = useUploadOwnerImage();
  const uploadImagePair = useUploadOwnerImagePair();
  const uploadVideo = useUploadOwnerVideo();
  const mediaPreviewUrl = useOwnerMediaPreviewUrl();
  const ownerChannel = useMemo(
    () => dashboard.data?.channels.find((channel) => channel.channel === "owner"),
    [dashboard.data],
  );

  useEffect(() => {
    if (!publishJob.data || !activeJobId) return;
    if (["succeeded", "failed", "rolled_back"].includes(publishJob.data.status)) {
      clearPersistedJob(activeJobId);
    }
  }, [activeJobId, clearPersistedJob, publishJob.data]);

  if (role !== "admin" && role !== "operator") {
    return <Alert type="error" showIcon message="无内容中心访问权限" description="业主托管内容只对管理员和运营开放。" />;
  }
  if (dashboard.error) {
    return (
      <Alert
        type="error"
        showIcon
        message="业主托管内容状态加载失败"
        description="暂时无法确认业主托管内容的线上状态，请重试后再进入编辑器。"
        action={<button type="button" className={ownerStyles.touchTarget} style={{ minWidth: 44, minHeight: 44 }} onClick={() => void dashboard.refetch()}>重新加载业主托管内容状态</button>}
      />
    );
  }
  if (ownerDraft.isLoading || dashboard.isLoading) {
    return <div aria-label="加载业主托管内容编辑器"><Skeleton active paragraph={{ rows: 8 }} /></div>;
  }
  if (ownerDraft.error || !ownerDraft.data) {
    return (
      <Alert
        type="error"
        showIcon
        message="业主草稿加载失败"
        description="本页没有创建本地替代草稿，以免覆盖服务器版本。请检查网络后刷新。"
        action={<button type="button" className={ownerStyles.touchTarget} style={{ minWidth: 44, minHeight: 44 }} onClick={() => void ownerDraft.refetch()}>重试加载业主草稿</button>}
      />
    );
  }

  return (
    <OwnerCaseEditor
      initialDraft={ownerDraft.data}
      role={role}
      recoveryScope={user?.user_id || role}
      publishedVersion={ownerChannel?.publishedVersion || null}
      onSave={(input) => saveDraft.mutateAsync(input)}
      onReload={ownerDraft.reload}
      onPreview={(revision) => previewDraft.mutateAsync(revision)}
      onUploadImage={(file, progress, signal) => uploadImage.mutateAsync({
        file,
        signal,
        onProgress: (percent) => {
          progress.transfer(percent);
          if (percent >= 100) progress.processing("服务端保存中");
        },
      })}
      onUploadImagePair={(file, progress, signal) => uploadImagePair.mutateAsync({
        file,
        signal,
        onProgress: (percent) => {
          progress.transfer(percent);
          if (percent >= 100) progress.processing("服务端生成缩略图与展示图");
        },
      })}
      onUploadVideo={(file, progress, signal) => uploadVideo.mutateAsync({ file, progress, signal })}
      onRequestMediaUrl={(mediaId) => mediaPreviewUrl.mutateAsync(mediaId)}
      onPublish={(input) => publishOwner.mutateAsync(input)}
      onJobStarted={trackJob}
      publishJob={publishJob.data || null}
      releases={releases.data?.releases || []}
      onRollback={(input) => rollbackOwner.mutateAsync(input)}
      onConflictCleared={saveDraft.clearConflict}
      onNavigate={(href) => router.push(href)}
      activeJobId={activeJobId}
      releasesStatus={releases.error ? "error" : releases.isLoading ? "loading" : "success"}
      onRetryReleases={() => void releases.refetch()}
      publishTrackingStatus={!activeJobId ? "idle" : publishJob.error ? "error" : publishJob.isLoading ? "loading" : "success"}
      publishTrackingError={publishJob.error instanceof Error ? publishJob.error.message : null}
      onRetryPublishTracking={() => void publishJob.refetch()}
    />
  );
}
