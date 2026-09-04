"use client";

import type { ContentDashboardChannel, ContentRelease, OwnerRelease } from "@/features/content/types";
import {
  ContentChannelCard,
  type PublishedAtState,
} from "@/components/content/ContentChannelCard";
import styles from "@/components/content/content-center.module.css";
import { useAuthStore } from "@/lib/auth";
import {
  useContentDashboard,
  useUpdateContentChannel,
} from "@/hooks/content/useContentPlatform";
import {
  useOwnerPublishJob,
  useOwnerReleases,
  usePersistentOwnerJob,
} from "@/hooks/content/useOwnerContent";
import { useStructuredContent } from "@/hooks/content/useStructuredContent";
import type { ContentChannel } from "@/features/content/types";

const EMPTY_CHANNELS: Record<ContentChannel, ContentDashboardChannel> = {
  owner: { channel: "owner", enabled: false, revision: 0, draftDirty: false, publishedVersion: null },
  stay_guide: { channel: "stay_guide", enabled: false, revision: 0, draftDirty: false, publishedVersion: null },
  travel: { channel: "travel", enabled: false, revision: 0, draftDirty: false, publishedVersion: null },
};

interface ReleaseQueryState {
  releases: Array<ContentRelease | OwnerRelease> | undefined;
  isLoading: boolean;
  isError: boolean;
}

function releaseTimeView(
  query: ReleaseQueryState,
  version: string | null,
): { publishedAt: string | null; publishedAtState: PublishedAtState } {
  if (!version) return { publishedAt: null, publishedAtState: "never" };
  if (query.isLoading) return { publishedAt: null, publishedAtState: "loading" };
  if (query.isError) return { publishedAt: null, publishedAtState: "error" };
  const release = query.releases?.find((item) => item.version === version);
  if (!release?.publishedAt) return { publishedAt: null, publishedAtState: "error" };
  return { publishedAt: release.publishedAt, publishedAtState: "ready" };
}

export default function ContentPage() {
  const user = useAuthStore((state) => state.user);
  const isAdmin = user?.role === "admin";
  const canEdit = isAdmin || user?.role === "operator";
  const dashboard = useContentDashboard();
  const updateChannel = useUpdateContentChannel();

  const ownerReleases = useOwnerReleases();
  const ownerPersistentJob = usePersistentOwnerJob(user?.user_id);
  const ownerPublishJob = useOwnerPublishJob(ownerPersistentJob.activeJobId);
  const stayGuide = useStructuredContent("stay_guide", user?.user_id);
  const travelGuide = useStructuredContent("travel", user?.user_id);

  if (!canEdit) {
    return (
      <main className={styles.page}>
        <div className={styles.pageState} role="alert">当前账号没有小程序内容管理权限。</div>
      </main>
    );
  }

  const channel = (name: ContentChannel) =>
    dashboard.data?.channels.find((item) => item.channel === name) || EMPTY_CHANNELS[name];
  const owner = channel("owner");
  const stay = channel("stay_guide");
  const travel = channel("travel");
  const ownerReleaseTime = releaseTimeView(
    {
      releases: ownerReleases.data?.releases,
      isLoading: ownerReleases.isLoading,
      isError: ownerReleases.isError,
    },
    owner.publishedVersion,
  );
  const stayReleaseTime = releaseTimeView(
    {
      releases: stayGuide.history.data?.releases,
      isLoading: stayGuide.history.isLoading,
      isError: stayGuide.history.isError,
    },
    stay.publishedVersion,
  );
  const travelReleaseTime = releaseTimeView(
    {
      releases: travelGuide.history.data?.releases,
      isLoading: travelGuide.history.isLoading,
      isError: travelGuide.history.isError,
    },
    travel.publishedVersion,
  );

  const changeEnabled = async (name: ContentChannel, enabled: boolean) => {
    await updateChannel.mutateAsync({ channel: name, enabled });
  };

  return (
    <main className={styles.page}>
      <header className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>小程序内容管理</h1>
        <p className={styles.pageExplanation}>在这里维护用户打开小程序后看到的内容。</p>
      </header>

      {dashboard.isLoading ? (
        <div className={styles.pageState} role="status">正在读取小程序内容状态…</div>
      ) : dashboard.isError ? (
        <div className={styles.pageState} role="alert">
          <div>暂时无法读取内容状态，请检查网络后重试。</div>
          <button className={styles.retryButton} type="button" onClick={() => void dashboard.refetch()}>
            重新读取
          </button>
        </div>
      ) : (
        <div className={styles.channelList}>
          <ContentChannelCard
            channel="owner"
            title="业主托管招商"
            description="给有闲置房源的业主了解托管服务，并提交合作登记。"
            href="/content/owner"
            enabled={owner.enabled}
            revision={owner.revision}
            publishedVersion={owner.publishedVersion}
            {...ownerReleaseTime}
            draftDirty={owner.draftDirty}
            publishJob={ownerPublishJob.data || null}
            isAdmin={isAdmin}
            isUpdating={updateChannel.isPending}
            onChangeEnabled={(enabled) => changeEnabled("owner", enabled)}
          />
          <ContentChannelCard
            channel="stay_guide"
            title="通用入住说明"
            description="给所有住客查看通用的入住、停车、设备、规则和联系说明。"
            href="/content/stay-guide"
            enabled={stay.enabled}
            revision={stay.revision}
            publishedVersion={stay.publishedVersion}
            {...stayReleaseTime}
            draftDirty={stay.draftDirty}
            publishJob={stayGuide.publishJob.data || null}
            isAdmin={isAdmin}
            isUpdating={updateChannel.isPending}
            onChangeEnabled={(enabled) => changeEnabled("stay_guide", enabled)}
          />
          <ContentChannelCard
            channel="travel"
            title="灵山湾旅游攻略"
            description="给住客查看灵山湾周边的景点、吃饭、亲子、交通和雨天推荐。"
            href="/content/travel-guide"
            enabled={travel.enabled}
            revision={travel.revision}
            publishedVersion={travel.publishedVersion}
            {...travelReleaseTime}
            draftDirty={travel.draftDirty}
            publishJob={travelGuide.publishJob.data || null}
            isAdmin={isAdmin}
            isUpdating={updateChannel.isPending}
            onChangeEnabled={(enabled) => changeEnabled("travel", enabled)}
          />
        </div>
      )}
    </main>
  );
}
