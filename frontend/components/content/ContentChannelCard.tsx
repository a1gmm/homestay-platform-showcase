"use client";

import { useState } from "react";
import Link from "next/link";
import { Modal } from "antd";
import type { ContentChannel } from "@/features/content/types";
import styles from "./content-center.module.css";

export type ContentOverviewStatus =
  | "未填写"
  | "有未发布修改"
  | "已上线"
  | "已暂停"
  | "发布处理中"
  | "发布失败";

export type PublishedAtState = "never" | "loading" | "error" | "ready";

export interface ChannelPublishJobSummary {
  jobId: string;
  status: string;
  progressStage?: string | null;
  targetVersion?: string | null;
}

export interface ContentChannelCardProps {
  channel: ContentChannel;
  title: string;
  description: string;
  href: string;
  enabled: boolean;
  revision: number;
  publishedVersion: string | null;
  publishedAt: string | null;
  publishedAtState: PublishedAtState;
  draftDirty: boolean;
  publishJob: ChannelPublishJobSummary | null;
  isAdmin: boolean;
  isUpdating: boolean;
  onChangeEnabled(enabled: boolean): Promise<unknown>;
}

const TERMINAL_JOB_STATUSES = new Set(["succeeded", "rolled_back"]);

const STATUS_COPY: Record<ContentOverviewStatus, { explanation: string; action: string }> = {
  未填写: {
    explanation: "还没有填写可发布的内容。",
    action: "开始填写",
  },
  有未发布修改: {
    explanation: "修改已经保存，还没有发布到小程序。",
    action: "继续编辑",
  },
  已上线: {
    explanation: "用户现在可以从小程序首页进入。",
    action: "管理内容",
  },
  已暂停: {
    explanation: "内容仍然保留，但用户暂时看不到入口。",
    action: "编辑内容",
  },
  发布处理中: {
    explanation: "系统正在更新小程序，请稍候。",
    action: "查看发布进度",
  },
  发布失败: {
    explanation: "本次发布没有完成，当前线上内容不受影响。",
    action: "检查并重试",
  },
};

export function getContentOverviewStatus({
  channel,
  enabled,
  revision,
  publishedVersion,
  draftDirty,
  publishJob,
}: Pick<
  ContentChannelCardProps,
  "channel" | "enabled" | "revision" | "publishedVersion" | "draftDirty" | "publishJob"
>): ContentOverviewStatus {
  if (publishJob?.status === "failed") return "发布失败";
  if (publishJob && !TERMINAL_JOB_STATUSES.has(publishJob.status)) return "发布处理中";
  if (revision === 0) return "未填写";
  if (!publishedVersion) return "有未发布修改";
  if (channel === "owner") return draftDirty ? "有未发布修改" : "已上线";
  if (!enabled) return "已暂停";
  if (draftDirty) return "有未发布修改";
  return "已上线";
}

function formatPublishedAt(value: string | null): string {
  if (!value) return "暂时无法读取";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间记录不可用";

  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((item) => item.type === type)?.value || "";
  return `${part("year")}年${part("month")}月${part("day")}日 ${part("hour")}:${part("minute")}`;
}

function publishedAtLabel(state: PublishedAtState, value: string | null): string {
  if (state === "never") return "从未发布";
  if (state === "loading") return "正在读取…";
  if (state === "error") return "暂时无法读取";
  return formatPublishedAt(value);
}

export function ContentChannelCard(props: ContentChannelCardProps) {
  const {
    channel,
    title,
    description,
    href,
    enabled,
    revision,
    publishedVersion,
    publishedAt,
    publishedAtState,
    publishJob,
    isAdmin,
    isUpdating,
    onChangeEnabled,
  } = props;
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [confirmationError, setConfirmationError] = useState<string | null>(null);
  const status = getContentOverviewStatus(props);
  const copy = STATUS_COPY[status];
  const nextEnabled = !enabled;
  const actionWord = enabled ? "暂停" : "重新开启";
  const titleId = `content-channel-${channel}`;

  const confirmChange = async () => {
    setConfirmationError(null);
    try {
      await onChangeEnabled(nextEnabled);
      setConfirmationOpen(false);
    } catch {
      setConfirmationError("操作未完成，请检查网络后重试。");
    }
  };

  return (
    <section
      className={styles.channelCard}
      aria-labelledby={titleId}
      data-testid="content-channel-card"
    >
      <div className={styles.cardTopline}>
        <div>
          <h2 id={titleId} className={styles.cardTitle}>{title}</h2>
          <p className={styles.cardDescription}>{description}</p>
        </div>
        <div className={styles.statusLine} aria-label={`当前状态：${status}`}>
          <span className={`${styles.statusDot} ${styles[`status${status}`]}`} aria-hidden="true" />
          <span>{status}</span>
        </div>
      </div>

      <p className={styles.statusExplanation}>
        {channel === "owner" && status === "已上线"
          ? "这是小程序的固定服务入口，用户始终可以进入。"
          : copy.explanation}
      </p>
      <p className={styles.publishedTime}>最近上线：{publishedAtLabel(publishedAtState, publishedAt)}</p>

      <div className={styles.cardActions}>
        <Link className={styles.primaryAction} href={href}>{copy.action}</Link>
        {channel !== "owner" && publishedVersion && isAdmin ? (
          <button
            type="button"
            className={styles.secondaryAction}
            onClick={() => {
              setConfirmationError(null);
              setConfirmationOpen(true);
            }}
            disabled={isUpdating}
            aria-label={`${actionWord}${title}`}
          >
            {actionWord}小程序入口
          </button>
        ) : null}
        {channel !== "owner" && publishedVersion && !isAdmin ? (
          <span className={styles.permissionNote}>入口开关由管理员操作</span>
        ) : null}
      </div>

      {isAdmin ? (
        <details className={styles.technicalDetails}>
          <summary>技术详情</summary>
          <dl className={styles.technicalList}>
            <div><dt>线上内容版本</dt><dd>{publishedVersion || "尚未发布"}</dd></div>
            <div><dt>当前内容修订</dt><dd>{revision}</dd></div>
            {publishJob ? <div><dt>发布任务编号</dt><dd>{publishJob.jobId}</dd></div> : null}
            {publishJob?.progressStage ? <div><dt>内部处理阶段</dt><dd>{publishJob.progressStage}</dd></div> : null}
            {publishJob?.targetVersion ? <div><dt>目标内容版本</dt><dd>{publishJob.targetVersion}</dd></div> : null}
          </dl>
        </details>
      ) : null}

      <Modal
        open={confirmationOpen}
        title={`${actionWord}${title}？`}
        okText={enabled ? "确认暂停" : "确认开启"}
        cancelText="取消"
        confirmLoading={isUpdating}
        onOk={confirmChange}
        onCancel={() => {
          setConfirmationError(null);
          setConfirmationOpen(false);
        }}
        rootClassName={styles.confirmationRoot}
      >
        <p className={styles.confirmationCopy}>
          {enabled
            ? "暂停后，用户将暂时无法在小程序首页进入这项服务。已发布内容不会删除。"
            : "开启后，用户会重新在小程序首页看到并进入这项服务。"}
        </p>
        {confirmationError ? <p className={styles.confirmationError} role="alert">{confirmationError}</p> : null}
      </Modal>
    </section>
  );
}
