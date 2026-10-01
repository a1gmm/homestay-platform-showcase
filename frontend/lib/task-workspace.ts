import dayjs from "dayjs";
import type { TaskOut } from "@/lib/types";

export function taskDisplayStatus(task: TaskOut): string {
  if (task.status === "done" || task.status === "cancelled") return task.status;
  if (task.review_status === "pending_review") return "pending_review";
  if (task.review_status === "rejected") return "in_progress";
  return task.status;
}

export function isActiveTask(task: TaskOut): boolean {
  return task.status !== "done" && task.status !== "cancelled";
}

export function selectTaskWorkspace(tasks: TaskOut[], filters: {
  status: string; keyword: string; assignee?: string; deadline?: string; overdueOnly: boolean;
  attention?: string;
}, now = Date.now()) {
  const keyword = filters.keyword.trim().toLowerCase();
  const attention = (task: TaskOut, key: string) => {
    if (!isActiveTask(task)) return false;
    if (key === "no_deadline") return !task.deadline;
    if (key === "unassigned") return !task.assignee_id;
    if (key === "aged") return Date.parse(task.created_at) <= now - 7 * 86400000;
    return !task.deadline || !task.assignee_id || Date.parse(task.created_at) <= now - 7 * 86400000 || Date.parse(task.deadline) < now;
  };
  const scoped = tasks.filter(task =>
    (!keyword || [task.title, task.room_id, task.order_id].some(v => v?.toLowerCase().includes(keyword))) &&
    (!filters.assignee || (filters.assignee === "unassigned" ? !task.assignee_id : task.assignee_id === filters.assignee)) &&
    (!filters.deadline || (task.deadline && dayjs(task.deadline).format("YYYY-MM-DD") === filters.deadline)) &&
    (!filters.attention || attention(task, filters.attention)) &&
    (!filters.overdueOnly || (isActiveTask(task) && task.deadline && Date.parse(task.deadline) < now)),
  );
  const items = scoped.filter(task => filters.status === "all" ||
    (filters.status === "active" ? isActiveTask(task) : taskDisplayStatus(task) === filters.status));
  return {
    items,
    counts: scoped.reduce<Record<string, number>>((counts, task) => {
      const status = taskDisplayStatus(task);
      counts[status] = (counts[status] || 0) + 1;
      return counts;
    }, {}),
    active: scoped.filter(isActiveTask).length,
    overdue: scoped.filter(task => isActiveTask(task) && task.deadline && Date.parse(task.deadline) < now).length,
    no_deadline: scoped.filter(task => attention(task, "no_deadline")).length,
    unassigned: scoped.filter(task => attention(task, "unassigned")).length,
    aged: scoped.filter(task => attention(task, "aged")).length,
    needs_attention: scoped.filter(task => attention(task, "needs_attention")).length,
  };
}
