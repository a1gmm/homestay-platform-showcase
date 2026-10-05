import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { tasksApi } from "@/lib/api";

type AttentionFilter = "no_deadline" | "unassigned" | "aged" | "needs_attention";

export function useTasksWorkspace() {
  const params = useSearchParams();
  const urlStatus = params.get("status");
  const urlOverdue = params.get("overdue") === "true";
  const urlAttention = params.get("attention");
  const [statusFilter, setStatusFilter] = useState(urlStatus || "active");
  const [overdueOnly, setOverdueOnly] = useState(urlOverdue);
  const [keyword, setKeyword] = useState("");
  const [searchKeyword, setSearchKeyword] = useState("");
  const [assignee, setAssignee] = useState<string>();
  const [deadline, setDeadline] = useState<string>();
  const [page, setPage] = useState(1);
  const [workScope, setWorkScope] = useState("current");
  const [attention, setAttention] = useState<AttentionFilter>();
  useEffect(() => {
    const timer = setTimeout(() => setSearchKeyword(keyword), 250);
    return () => clearTimeout(timer);
  }, [keyword]);
  useEffect(() => {
    setStatusFilter(urlStatus || "active"); setOverdueOnly(urlOverdue);
    setAttention(["no_deadline", "unassigned", "aged", "needs_attention"].includes(urlAttention || "") ? urlAttention as AttentionFilter : undefined);
  }, [urlStatus, urlOverdue, urlAttention]);
  useEffect(() => { setPage(1); }, [statusFilter, overdueOnly, searchKeyword, assignee, deadline, attention, workScope]);
  const filters = { status: statusFilter, keyword: searchKeyword, assignee_id: assignee, deadline,
    overdue_only: overdueOnly, attention, page, page_size: 30, work_scope: workScope };
  const query = useQuery({ queryKey: ["tasks", "workspace", filters], queryFn: () => tasksApi.workspace(filters).then(r => r.data) });
  const workspace = query.data || { items: [], total: 0, counts: {}, active: 0, overdue: 0,
    no_deadline: 0, unassigned: 0, aged: 0, needs_attention: 0, page: 1, page_size: 30 };
  return { ...workspace, query, statusFilter, setStatusFilter, overdueOnly, setOverdueOnly,
    keyword, setKeyword, assignee, setAssignee, deadline, setDeadline, attention, setAttention,
    setPage, workScope, setWorkScope, visibleItems: workspace.items };
}
