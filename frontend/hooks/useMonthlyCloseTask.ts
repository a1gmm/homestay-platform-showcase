import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { getPersistedAuthSessionId } from "@/lib/auth";
import { monthlyCloseApi } from "@/lib/api";
import type { MonthlyCloseTask, MonthlyCloseTaskCommand } from "@/lib/monthly-close-task";

type TaskState = { scope: string; task: MonthlyCloseTask | null; loading: boolean; pending: boolean; error: string | null };
const initial = (scope: string): TaskState => ({ scope, task: null, loading: true, pending: false, error: null });

export function useMonthlyCloseTask(month: string, scopeKey: string, enabled: boolean, refreshKey: string) {
  const scope = `${scopeKey}:${month}:${enabled}`;
  const [state, setState] = useState<TaskState>(() => initial(scope));
  const active = useRef("");
  const requestSession = useRef<string | null>(null);
  const readController = useRef<AbortController | null>(null);
  const writeController = useRef<AbortController | null>(null);
  const pending = useRef(false);
  const sequence = useRef(0);
  const latestTask = useRef<MonthlyCloseTask | null>(null);
  useLayoutEffect(() => {
    active.current = scope;
    requestSession.current = getPersistedAuthSessionId();
    latestTask.current = null;
    pending.current = false;
    setState(initial(scope));
    return () => {
      active.current = "";
      sequence.current += 1;
      readController.current?.abort();
      writeController.current?.abort();
    };
  }, [scope]);
  const sessionMatches = useCallback(() => {
    if (getPersistedAuthSessionId() === requestSession.current) return true;
    if (active.current === scope) {
      latestTask.current = null;
      setState({ scope, task: null, loading: false, pending: false, error: "账号已切换，请重新打开当前账号的月结任务。" });
    }
    return false;
  }, [scope]);
  const accept = useCallback((task: MonthlyCloseTask | null) => {
    if (task && latestTask.current?.task_id === task.task_id && task.revision < latestTask.current.revision) {
      // Keep the newer result, but re-arm polling after an out-of-date replica read.
      setState((current) => ({ ...current, loading: false }));
      return;
    }
    latestTask.current = task;
    setState((current) => ({ ...current, scope, task, loading: false, error: null }));
  }, [scope]);
  const refresh = useCallback(async () => {
    if (!enabled || active.current !== scope || pending.current || !sessionMatches()) return;
    readController.current?.abort();
    const controller = new AbortController();
    readController.current = controller;
    const request = ++sequence.current;
    try {
      const response = await monthlyCloseApi.getTask(month, controller.signal);
      if (active.current === scope && !controller.signal.aborted && sequence.current === request && sessionMatches()) accept(response.data.task);
    } catch (error) {
      if (active.current === scope && !controller.signal.aborted && sequence.current === request && sessionMatches()) {
        const status = (error as { response?: { status?: number } })?.response?.status;
        const unavailable = [401, 403, 404].includes(status ?? 0);
        if (unavailable) latestTask.current = null;
        setState((current) => ({ ...current, ...(unavailable ? { task: null } : {}), loading: false,
          error: unavailable ? "当前账号无法读取这项任务，请重新核对账号与月份。" : "任务状态暂时没有更新，请重试。" }));
      }
    }
  }, [accept, enabled, month, scope, sessionMatches]);
  useEffect(() => { void refresh(); }, [refresh, refreshKey]);
  const visible = state.scope === scope ? state : initial(scope);
  useEffect(() => {
    if (!enabled || visible.pending || visible.loading) return;
    const status = visible.task?.status;
    const delay = visible.error ? 15_000 : status === "queued" || status === "running" ? 3_000
      : status === "waiting_user" || status === "waiting_approval" ? 15_000 : null;
    if (delay === null) return;
    const timer = window.setTimeout(() => { void refresh(); }, delay);
    return () => window.clearTimeout(timer);
  }, [enabled, refresh, state, visible.error, visible.loading, visible.pending, visible.task?.status]);
  const command = useCallback(async (input: Omit<MonthlyCloseTaskCommand, "expected_revision">) => {
    if (!enabled || active.current !== scope || pending.current || !sessionMatches()) return null;
    pending.current = true;
    readController.current?.abort();
    sequence.current += 1;
    const controller = new AbortController();
    writeController.current = controller;
    setState((current) => ({ ...current, pending: true, error: null }));
    try {
      const response = await monthlyCloseApi.updateTask(month, { ...input, ...(latestTask.current ? { expected_revision: latestTask.current.revision } : {}) }, controller.signal);
      if (active.current !== scope || controller.signal.aborted || !sessionMatches()) return null;
      accept(response.data.task);
      return response.data.task;
    } catch (error) {
      if (active.current !== scope || controller.signal.aborted || !sessionMatches()) return null;
      const status = (error as { response?: { status?: number } })?.response?.status;
      setState((current) => ({ ...current, loading: false, error: status === 409 ? "任务已有新进展，已重新读取，请核对后再操作。" : "操作未完成，请重试；已保存的任务会保留。" }));
      if (status === 409) {
        pending.current = false;
        await refresh();
      }
      return null;
    } finally {
      if (active.current === scope && !controller.signal.aborted) {
        pending.current = false;
        setState((current) => ({ ...current, pending: false }));
      }
    }
  }, [accept, enabled, month, refresh, scope, sessionMatches]);
  return { ...visible, refresh, command };
}
