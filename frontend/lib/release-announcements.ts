export interface ReleaseAnnouncement {
  announcement_id: string;
  title: string;
  summary: string;
  items: string[];
  published_at: string;
  cta_label: string | null;
  cta_path: string | null;
}

export interface ReleaseAnnouncementList {
  items: ReleaseAnnouncement[];
}

export interface ReleaseAnnouncementClient {
  unread(): Promise<ReleaseAnnouncement[]>;
  acknowledge(announcementIds: string[]): Promise<string[]>;
}
