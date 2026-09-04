"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Modal } from "antd";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { extractErrorMessage } from "@/lib/api-errors";
import {
  getSnoozedAnnouncementIds,
  snoozeAnnouncements,
} from "@/lib/release-announcement-snooze";
import type {
  ReleaseAnnouncement,
  ReleaseAnnouncementClient,
} from "@/lib/release-announcements";

interface ReleaseAnnouncementGateProps {
  userId: string;
  client: ReleaseAnnouncementClient;
}

interface AcknowledgeSubmission {
  userId: string;
  announcementIds: string[];
  client: ReleaseAnnouncementClient;
}

const INTERNAL_CTA_ORIGIN = "https://release-announcement.internal";

function isSafeInternalCtaPath(path: string | null): path is string {
  if (
    !path ||
    path[0] !== "/" ||
    path[1] === "/" ||
    path.includes("\\") ||
    /[\u0000-\u001f\u007f]/.test(path)
  ) {
    return false;
  }

  try {
    return new URL(path, INTERNAL_CTA_ORIGIN).origin === INTERNAL_CTA_ORIGIN;
  } catch {
    return false;
  }
}

export function ReleaseAnnouncementGate({
  userId,
  client,
}: ReleaseAnnouncementGateProps) {
  const queryClient = useQueryClient();
  const titleRef = useRef<HTMLSpanElement>(null);
  const pendingUserIdsRef = useRef<Set<string>>(new Set());
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(() => new Set());
  const [pendingUserIds, setPendingUserIds] = useState<Set<string>>(() => new Set());
  const [errorsByUserId, setErrorsByUserId] = useState<Map<string, unknown>>(
    () => new Map(),
  );

  useEffect(() => {
    setDismissedIds(new Set());
  }, [userId]);

  const queryKey = ["release-announcements", userId] as const;
  const unreadQuery = useQuery({
    queryKey,
    queryFn: () => client.unread(),
    enabled: Boolean(userId),
    refetchInterval: false,
    meta: { silent: true },
  });

  const visibleItems = useMemo(() => {
    const snoozedIds = getSnoozedAnnouncementIds(userId);
    return (unreadQuery.data ?? []).filter(
      (item) => !snoozedIds.has(item.announcement_id) && !dismissedIds.has(item.announcement_id),
    );
  }, [dismissedIds, unreadQuery.data, userId]);

  const acknowledgeMutation = useMutation({
    mutationFn: ({ client: submittedClient, announcementIds }: AcknowledgeSubmission) =>
      submittedClient.acknowledge(announcementIds),
    meta: { silent: true },
    onSuccess: (_acknowledgedIds, submission) => {
      const submitted = new Set(submission.announcementIds);
      queryClient.setQueryData<ReleaseAnnouncement[]>(
        ["release-announcements", submission.userId],
        (current = []) =>
          current.filter((item) => !submitted.has(item.announcement_id)),
      );
      setErrorsByUserId((current) => {
        const next = new Map(current);
        next.delete(submission.userId);
        return next;
      });
    },
    onError: (error, submission) => {
      setErrorsByUserId((current) => new Map(current).set(submission.userId, error));
    },
    onSettled: (_data, _error, submission) => {
      pendingUserIdsRef.current.delete(submission.userId);
      setPendingUserIds((current) => {
        const next = new Set(current);
        next.delete(submission.userId);
        return next;
      });
    },
  });

  const displayedIds = visibleItems.map((item) => item.announcement_id);

  useEffect(() => {
    if (visibleItems.length > 0) titleRef.current?.focus();
  }, [visibleItems.length]);

  const snoozeDisplayed = () => {
    if (pendingUserIdsRef.current.has(userId)) return;
    if (displayedIds.length === 0) return;
    snoozeAnnouncements(userId, displayedIds);
    setDismissedIds((current) => new Set([...Array.from(current), ...displayedIds]));
  };

  const acknowledgeDisplayed = () => {
    if (pendingUserIdsRef.current.has(userId) || displayedIds.length === 0) return;
    pendingUserIdsRef.current.add(userId);
    setPendingUserIds((current) => new Set(current).add(userId));
    setErrorsByUserId((current) => {
      const next = new Map(current);
      next.delete(userId);
      return next;
    });
    acknowledgeMutation.mutate({
      userId,
      announcementIds: displayedIds,
      client,
    });
  };

  if (unreadQuery.isError || visibleItems.length === 0) return null;

  const isAcknowledging = pendingUserIds.has(userId);
  const acknowledgeError = errorsByUserId.get(userId);

  return (
    <Modal
      title={
        <span ref={titleRef} tabIndex={-1}>
          系统更新
        </span>
      }
      open
      width="calc(100vw - 32px)"
      style={{ maxWidth: 560 }}
      destroyOnHidden
      maskClosable={false}
      closable={isAcknowledging ? { disabled: true } : true}
      onCancel={snoozeDisplayed}
      afterOpenChange={(open) => {
        if (open) titleRef.current?.focus();
      }}
      footer={
        <>
          <Button disabled={isAcknowledging} onClick={snoozeDisplayed}>
            稍后提醒
          </Button>
          <Button
            type="primary"
            loading={isAcknowledging}
            disabled={isAcknowledging}
            onClick={acknowledgeDisplayed}
          >
            我知道了
          </Button>
        </>
      }
    >
      {acknowledgeError ? (
        <Alert
          type="error"
          showIcon
          message={extractErrorMessage(acknowledgeError)}
          style={{ marginBottom: 16 }}
        />
      ) : null}

      {visibleItems.map((announcement) => {
        const hasSafeCta =
          announcement.cta_label && isSafeInternalCtaPath(announcement.cta_path);

        return (
          <section key={announcement.announcement_id} style={{ marginBottom: 20 }}>
            <h3>{announcement.title}</h3>
            <p>{announcement.summary}</p>
            <ul>
              {announcement.items.map((item, index) => (
                <li key={`${announcement.announcement_id}-${index}`}>{item}</li>
              ))}
            </ul>
            {hasSafeCta ? (
              <Link href={announcement.cta_path!}>{announcement.cta_label}</Link>
            ) : null}
          </section>
        );
      })}
    </Modal>
  );
}
