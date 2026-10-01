export type MonthlyCloseTaskStatus = "queued" | "running" | "waiting_user" | "waiting_approval" | "paused" | "succeeded" | "failed";
export interface MonthlyCloseTask {
  task_id: string;
  revision: number;
  status: MonthlyCloseTaskStatus;
  goal: string;
  summary: string;
  steps: Array<{ key: string; label: string; status: string; detail?: string }>;
  questions: Array<{ key: string; subject: string; message: string; source_id?: string; step_key?: string }>;
  actions: Array<{ key: string; label: string; text: string; proposal_id?: string; context_run_id?: string; attachment_ids?: string[] }>;
  artifacts: Array<{ label: string; url: string; kind: string }>;
  updated_at: string;
  activity?: string;
}
export interface MonthlyCloseTaskCommand {
  action: "start" | "resume" | "pause";
  goal?: string;
  expected_revision?: number;
}

// Only same API monthly-close download endpoints are accepted. Never navigate to a
// server-supplied absolute URL or pass it to the authenticated HTTP client.
export function monthlyCloseTaskArtifactPath(url: string, month: string): string | null {
  if (!/^\d{4}-\d{2}$/.test(month) || /[\\\s#]/.test(url)) return null;
  const path = url.startsWith("/api/v1/") ? url.slice(7) : url;
  if (/^\/export\/settlements\/[A-Za-z0-9_-]+\/(?:package(?:\?internal=(?:true|false))?|income-detail)$/.test(path)) return path;
  if (!path.startsWith(`/monthly-close/${month}/`) || /%(?:2e|2f|5c)|\.\./i.test(path)) return null;
  const pathname = path.split("?")[0];
  if (!/(?:\/export|\/download|\/package|\/artifacts\/[^/]+)$/.test(pathname)) return null;
  return path;
}
