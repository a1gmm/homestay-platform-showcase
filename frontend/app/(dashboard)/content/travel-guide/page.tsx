"use client";

import { Alert, Skeleton } from "antd";
import { useMemo } from "react";
import { useRouter } from "next/navigation";
import { TravelGuideEditor } from "@/components/content/TravelGuideEditor";
import { useContentDashboard } from "@/hooks/content/useContentPlatform";
import { useStructuredContent } from "@/hooks/content/useStructuredContent";
import { useAuthStore } from "@/lib/auth";

const retryStyle = { minWidth: 44, minHeight: 44 };

export default function TravelGuideContentPage() {
  const user = useAuthStore((state) => state.user);
  const role = user?.role;
  const router = useRouter();
  const dashboard = useContentDashboard();
  const structured = useStructuredContent("travel", user?.user_id);
  const channel = useMemo(() => dashboard.data?.channels.find((item) => item.channel === "travel"), [dashboard.data]);

  if (role !== "admin" && role !== "operator") return <Alert type="error" showIcon message="无内容中心访问权限" description="灵山湾旅游攻略只对管理员和运营开放。" />;
  if (dashboard.error) return <Alert type="error" showIcon message="灵山湾旅游攻略状态加载失败" description="暂时无法确认线上状态，请检查网络后重试。" action={<button type="button" style={retryStyle} onClick={() => void dashboard.refetch()}>重新加载灵山湾旅游攻略状态</button>} />;
  if (dashboard.isLoading || structured.draft.isLoading) return <div aria-label="加载灵山湾旅游攻略编辑器"><Skeleton active paragraph={{ rows: 8 }} /></div>;
  if (structured.draft.error || !structured.draft.data) return <Alert type="error" showIcon message="灵山湾旅游攻略草稿加载失败" description="没有创建本地替代草稿，以免覆盖服务器内容。请检查网络后重试。" action={<button type="button" style={retryStyle} onClick={() => void structured.draft.refetch()}>重试加载灵山湾旅游攻略草稿</button>} />;

  return <TravelGuideEditor
    initialDraft={structured.draft.data}
    role={role}
    publishedVersion={channel?.publishedVersion || null}
    conflict={structured.save.conflict}
    activeJobId={structured.activeJobId}
    publishJob={structured.publishJob.data || null}
    releases={structured.history.data?.releases || []}
    releasesStatus={structured.history.error ? "error" : structured.history.isLoading ? "loading" : "success"}
    publishTrackingStatus={!structured.activeJobId ? "idle" : structured.publishJob.error ? "error" : structured.publishJob.isLoading ? "loading" : "success"}
    publishTrackingError={structured.publishJob.error instanceof Error ? structured.publishJob.error.message : null}
    onSave={(input) => structured.save.mutateAsync(input)}
    onRefresh={structured.refreshDraft}
    onClearConflict={structured.save.clearConflict}
    onPreview={(revision) => structured.preview.mutateAsync(revision)}
    onPublish={(input) => structured.publish.mutateAsync(input)}
    onJobStarted={(accepted) => structured.resumePublishJob(accepted.jobId)}
    onRollback={(input) => structured.rollback.mutateAsync(input)}
    onRetryReleases={() => void structured.history.refetch()}
    onRetryPublishTracking={() => void structured.publishJob.refetch()}
    onNavigate={(href) => router.push(href)}
  />;
}
