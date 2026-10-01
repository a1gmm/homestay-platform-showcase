import type { AssistantProgress } from "@/lib/monthly-close-chat-stream";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { monthlyCloseApi, settlementsApi } from "@/lib/api";
import type { AssistantReply, MonthlyCloseCycle, MonthlyCloseEvent, MonthlyCloseInboxItem, MonthlyCloseIntakeLink, MonthlyCloseLayoutMemory, MonthlyCloseLayoutMetrics, MonthlyCloseOverview, MonthlyCloseProjectedDocument, MonthlyCloseProjectedDocumentSummary, MonthlyCloseProjection, MonthlyCloseReceiptView, MonthlyCloseSourceProposalQueue } from "@/lib/monthly-close";

function isNotFound(error: unknown): boolean {
  return (error as { response?: { status?: number } } | null)?.response?.status === 404;
}

function errorStatus(error: unknown): number | undefined {
  return (error as { response?: { status?: number } } | null)?.response?.status;
}

function errorCode(error: unknown): string | undefined {
  const detail = (error as { response?: { data?: { detail?: string | { code?: string } } } } | null)?.response?.data?.detail;
  return typeof detail === "object" ? detail.code : undefined;
}

export interface MonthlyCloseAuthScope {
  channel: "dashboard" | "staff";
  sessionId: string;
  userId: string;
  role: string;
}

function snapshotScopeKey(authScopeKey: string, snapshot: MonthlyCloseProjection) {
  return `${authScopeKey}:${snapshot.cycle_id}:${snapshot.billing_month}:${snapshot.actor_role}`;
}

function snapshotMatchesScope(snapshot: MonthlyCloseProjection, storedScope: string, authScopeKey: string, month: string, currentRole?: string) {
  return snapshot.billing_month === month
    && (!currentRole || snapshot.actor_role === currentRole)
    && storedScope === snapshotScopeKey(authScopeKey, snapshot);
}

export type MonthlyCloseWorkspaceApi = Pick<typeof monthlyCloseApi,
  "getProjection" | "listEvents" | "postMessage" | "getProjectionDocument" | "uploadDocument" | "receiveInbox"> & Partial<Pick<typeof monthlyCloseApi, "listMessages" | "postMessageStream">>;

export function mergeMonthlyCloseEvents(current: MonthlyCloseEvent[], incoming: MonthlyCloseEvent[]) {
  const merged = [...current];
  const indexes = new Map(merged.map((event, index) => [event.event_id, index]));
  for (const event of incoming) {
    const existing = indexes.get(event.event_id);
    if (existing === undefined) {
      indexes.set(event.event_id, merged.length);
      merged.push(event);
    } else {
      merged[existing] = event;
    }
  }
  return merged;
}

export function useMonthlyClose(month: string, enabled: boolean, managementEnabled = false, legacyEnabled = enabled, authScope?: MonthlyCloseAuthScope, workspaceApi: MonthlyCloseWorkspaceApi = monthlyCloseApi) {
  const queryClient = useQueryClient();
  const [visibleProjectionState, setVisibleProjectionState] = useState<{ snapshot: MonthlyCloseProjection; scope: string } | null>(null);
  const [events, setEvents] = useState<MonthlyCloseEvent[]>([]);
  const [replies, setReplies] = useState<AssistantReply[]>([]);
  const [durableReceipts, setDurableReceipts] = useState<MonthlyCloseReceiptView[]>([]);
  const [eventCursor, setEventCursor] = useState("");
  const [replayTick, setReplayTick] = useState(0);
  const snapshotIdentity = useRef("");
  const acceptedProjectionScope = useRef("");
  const deliveredEventIds = useRef(new Set<string>());
  const authChannel = authScope?.channel ?? "dashboard";
  const authSessionId = authScope?.sessionId ?? "legacy-session";
  const authUserId = authScope?.userId ?? "legacy-user";
  const authRole = authScope?.role ?? "legacy-role";
  const authScopeKey = `${authChannel}:${authSessionId}:${authUserId}:${authRole}`;
  const authScopeParts = useMemo(
    () => [authChannel, authSessionId, authUserId, authRole],
    [authChannel, authRole, authSessionId, authUserId],
  );
  const previousAuthScope = useRef(authScopeKey);
  const queryKey = ["monthly-close", month] as const;
  const projectionKey = useMemo(
    () => ["monthly-close-projection", ...authScopeParts, month] as const,
    [authScopeParts, month],
  );
  const sourceProposalKey = useMemo(
    () => ["monthly-close-source-proposals", ...authScopeParts, month] as const,
    [authScopeParts, month],
  );
  const inboxKey = ["monthly-close-inbox", month] as const;
  const overviewKey = ["monthly-close-overview"] as const;
  const linksKey = ["monthly-close-intake-links", month] as const;
  const memoriesKey = ["monthly-close-layout-memories"] as const;
  const metricsKey = ["monthly-close-layout-memory-metrics"] as const;
  const clearSensitiveState = useCallback(() => {
    setVisibleProjectionState(null);
    setEvents([]);
    setReplies([]);
    setDurableReceipts([]);
    setEventCursor("");
    snapshotIdentity.current = "";
    acceptedProjectionScope.current = "";
    deliveredEventIds.current.clear();
    queryClient.removeQueries({ queryKey: ["monthly-close-evidence"] });
  }, [queryClient]);

  useEffect(() => {
    if (previousAuthScope.current !== authScopeKey) {
      previousAuthScope.current = authScopeKey;
      clearSensitiveState();
    }
  }, [authScopeKey, clearSensitiveState]);
  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey }),
      queryClient.invalidateQueries({ queryKey: projectionKey, exact: true }),
      queryClient.invalidateQueries({ queryKey: inboxKey }),
      queryClient.invalidateQueries({ queryKey: overviewKey }),
      queryClient.invalidateQueries({ queryKey: sourceProposalKey, exact: true }),
    ]);
  };
  const overview = useQuery<MonthlyCloseOverview[]>({
    queryKey: overviewKey,
    enabled: enabled && managementEnabled,
    refetchInterval: enabled && managementEnabled ? 120_000 : false,
    queryFn: async () => (await monthlyCloseApi.overview()).data,
  });
  const cycle = useQuery<MonthlyCloseCycle | null>({
    queryKey,
    enabled: enabled && legacyEnabled && !!month,
    refetchInterval: enabled ? 30_000 : false,
    queryFn: async () => {
      try {
        return (await monthlyCloseApi.get(month)).data;
      } catch (error) {
        if (!isNotFound(error)) throw error;
        return null;
      }
    },
  });
  const projection = useQuery<MonthlyCloseProjection>({
    queryKey: projectionKey,
    enabled: enabled && !!month,
    staleTime: 15_000,
    queryFn: async () => {
      try {
        return (await workspaceApi.getProjection(month)).data;
      } catch (error) {
        if ([401, 403, 404].includes(errorStatus(error) ?? 0)) {
          clearSensitiveState();
        }
        throw error;
      }
    },
  });
  const sourceProposals = useQuery<MonthlyCloseSourceProposalQueue>({
    queryKey: sourceProposalKey,
    enabled: enabled && !!month && projection.data?.features.source_adapters === true
      && ["admin", "finance", "operator"].includes(authScope?.role ?? ""),
    staleTime: 5_000,
    queryFn: async () => (await monthlyCloseApi.listSourceProposals(month)).data,
  });
  const refetchProjection = projection.refetch;

  useEffect(() => {
    const snapshot = projection.data;
    if (!snapshot) return;
    const scope = snapshotScopeKey(authScopeKey, snapshot);
    if (!snapshotMatchesScope(snapshot, scope, authScopeKey, month, authScope?.role)) {
      clearSensitiveState();
      return;
    }
    const identity = `${scope}:${snapshot.input_hash}:${snapshot.snapshot_through_sequence}`;
    if (acceptedProjectionScope.current && acceptedProjectionScope.current !== scope) {
      setEvents([]);
      setReplies([]);
      setDurableReceipts([]);
      deliveredEventIds.current.clear();
      queryClient.removeQueries({ queryKey: ["monthly-close-evidence"] });
    }
    acceptedProjectionScope.current = scope;
    if (snapshotIdentity.current !== identity) {
      snapshotIdentity.current = identity;
    }
    setVisibleProjectionState({ snapshot, scope });
    setEventCursor(snapshot.snapshot_through_sequence);
    const canonicalDocuments = snapshot.sources.flatMap((source) => source.documents);
    setDurableReceipts((current) => current.filter((receipt) => !canonicalDocuments.some((document) =>
      (receipt.document_id && document.document_id === receipt.document_id)
      || (document.receipt_id && document.receipt_id === receipt.receipt_id),
    )));
  }, [authScope?.role, authScopeKey, clearSensitiveState, month, projection.data, queryClient]);

  const scopedVisibleProjection = visibleProjectionState
    && snapshotMatchesScope(visibleProjectionState.snapshot, visibleProjectionState.scope, authScopeKey, month, authScope?.role)
    ? visibleProjectionState.snapshot
    : null;

  useEffect(() => {
    const activeCycleId = scopedVisibleProjection?.cycle_id;
    if (!enabled || !activeCycleId || !eventCursor) return;
    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const scheduleReplay = () => {
      retryTimer = setTimeout(() => {
        if (!cancelled) setReplayTick((current) => current + 1);
      }, 2_000);
    };
    const replay = async () => {
      try {
        const response = await workspaceApi.listEvents(month, eventCursor);
        if (cancelled) return;
        const freshEvents = response.data.events.filter(
          (event) => !deliveredEventIds.current.has(event.event_id),
        );
        for (const event of freshEvents) deliveredEventIds.current.add(event.event_id);
        setEvents((current) => mergeMonthlyCloseEvents(current, response.data.events));
        if (response.data.next_cursor && response.data.next_cursor !== eventCursor) {
          setEventCursor(response.data.next_cursor);
        } else {
          scheduleReplay();
        }
        if (freshEvents.some((event) => (
          !event.kind.startsWith("assistant.")
          && event.kind !== "file.stored"
        ))) {
          await queryClient.invalidateQueries({ queryKey: projectionKey, exact: true });
        }
      } catch (error) {
        if (cancelled) return;
        const status = errorStatus(error);
        if (status === 410) {
          const fresh = await refetchProjection();
          if (cancelled) return;
          if (!fresh.isSuccess || !fresh.data) {
            clearSensitiveState();
            return;
          }
          const freshScope = fresh.data ? snapshotScopeKey(authScopeKey, fresh.data) : "";
          if (snapshotMatchesScope(fresh.data, freshScope, authScopeKey, month, authScope?.role)) {
            snapshotIdentity.current = "";
            acceptedProjectionScope.current = freshScope;
            queryClient.removeQueries({ queryKey: ["monthly-close-evidence"] });
            setVisibleProjectionState({ snapshot: fresh.data, scope: freshScope });
            setEvents([]);
            setEventCursor(fresh.data.snapshot_through_sequence);
          } else {
            clearSensitiveState();
          }
        } else if (status === 422 && ["EVENT_CURSOR_INVALID", "EVENT_CURSOR_REFRESH_REQUIRED"].includes(errorCode(error) ?? "")) {
          clearSensitiveState();
          void refetchProjection();
        } else if ([401, 403, 404].includes(status ?? 0)) {
          clearSensitiveState();
          void refetchProjection();
        } else {
          scheduleReplay();
        }
      }
    };
    void replay();
    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, [authScope?.role, authScopeKey, clearSensitiveState, enabled, eventCursor, month, projectionKey, queryClient, refetchProjection, replayTick, scopedVisibleProjection?.cycle_id, workspaceApi]);

  const history = useQuery({
    queryKey: ["monthly-close-chat-history", authScopeKey, month],
    enabled: enabled && !!scopedVisibleProjection && authScope?.role === "admin" && !!workspaceApi.listMessages,
    queryFn: async () => (await workspaceApi.listMessages!(month)).data.replies,
    staleTime: 15_000,
  });
  useEffect(() => {
    if (!history.data || !scopedVisibleProjection) return;
    setReplies((current) => {
      const restored = history.data!;
      const ids = new Set(restored.map((reply) => reply.run_id));
      return [...restored, ...current.filter((reply) => !ids.has(reply.run_id))];
    });
  }, [history.data, scopedVisibleProjection]);

  const [assistantProgress, setAssistantProgress] = useState<AssistantProgress | null>(null);
  const activeChat = useRef<AbortController | null>(null);
  const stopMessage = () => activeChat.current?.abort();
  useEffect(() => () => { activeChat.current?.abort(); }, []);
  const sendMessage = useMutation({
    mutationFn: async ({ text, attachmentIds = [], contextRunId }: { text: string; attachmentIds?: string[]; contextRunId?: string }) => {
      const requestScope = acceptedProjectionScope.current;
      const lastReply = replies[replies.length - 1];
      const context = contextRunId ?? (["investigate_cleaning", "cleaning_work_chat", "review_month", "agent_clarification", "get_document_status"].includes(lastReply?.tool ?? "") ? lastReply?.run_id : undefined);
      setAssistantProgress(null);
      const controller = new AbortController();
      activeChat.current = controller;
      try {
        const response = workspaceApi.postMessageStream
          ? await workspaceApi.postMessageStream(month, text, attachmentIds, context, (progress) => {
            if (!controller.signal.aborted && requestScope === acceptedProjectionScope.current) setAssistantProgress(progress);
          }, controller.signal)
          : context
          ? await workspaceApi.postMessage(month, text, attachmentIds, context)
          : await workspaceApi.postMessage(month, text, attachmentIds);
        if (requestScope !== acceptedProjectionScope.current) throw new Error("月份或账号已切换，请在当前月份重新查询");
        return response;
      } catch (error) {
        if (controller.signal.aborted) {
          void queryClient.invalidateQueries({ queryKey: ["monthly-close-chat-history", authScopeKey, month], exact: true });
          throw new Error("已停止等待本次回答。已保存的记录仍保留；可以重新提问，或刷新查看最后结果。");
        }
        throw error;
      } finally {
        if (activeChat.current === controller) activeChat.current = null;
      }
    },
    onSuccess: async (response) => {
      setReplies((current) => current.some((reply) => reply.run_id === response.data.run_id)
        ? current
        : [...current, response.data]);
      await queryClient.invalidateQueries({ queryKey: projectionKey, exact: true });
    },
  });
  const loadEvidence = async (documentId: string): Promise<MonthlyCloseProjectedDocument> => {
    const cycleId = scopedVisibleProjection?.cycle_id;
    if (!cycleId) throw new Error("当前月结内容不可用");
    return queryClient.fetchQuery({
      queryKey: ["monthly-close-evidence", ...authScopeParts, cycleId, documentId],
      queryFn: async () => (await workspaceApi.getProjectionDocument(month, documentId)).data,
      staleTime: 15_000,
    });
  };
  const retryAnalysis = useMutation({
    mutationFn: async (document: MonthlyCloseProjectedDocumentSummary) => {
      if (document.source_type === "ota_statement") { await monthlyCloseApi.analyzeOta(month, document.document_id); return; }
      if (["cleaning_statement", "linen_statement"].includes(document.source_type)) { await monthlyCloseApi.analyzeServiceStatement(month, document.document_id); return; }
      if (["utility_receipt", "utility_expense"].includes(document.source_type)) { await monthlyCloseApi.analyzeUtilityStatement(month, document.document_id); return; }
      if (document.source_type === "operating_expenses") { await monthlyCloseApi.analyzeOperatingExpense(month, document.document_id); return; }
      throw new Error("该资料暂不支持自动重试");
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: projectionKey, exact: true });
    },
  });
  const start = useMutation({
    mutationFn: () => monthlyCloseApi.start(month),
    onSuccess: async (response) => {
      queryClient.setQueryData(queryKey, response.data);
      await queryClient.invalidateQueries({ queryKey: overviewKey });
    },
  });
  const inbox = useQuery<MonthlyCloseInboxItem[]>({
    queryKey: inboxKey,
    enabled: enabled && !!month && cycle.data != null,
    refetchInterval: enabled ? 30_000 : false,
    queryFn: async () => (await monthlyCloseApi.listInbox(month)).data,
  });
  const intakeLinks = useQuery<MonthlyCloseIntakeLink[]>({
    queryKey: linksKey,
    enabled: enabled && managementEnabled && !!month && cycle.data != null,
    queryFn: async () => (await monthlyCloseApi.listIntakeLinks(month)).data,
  });
  const layoutMemories = useQuery<MonthlyCloseLayoutMemory[]>({
    queryKey: memoriesKey,
    enabled: enabled && managementEnabled,
    queryFn: async () => (await monthlyCloseApi.listLayoutMemories()).data,
  });
  const layoutMetrics = useQuery<MonthlyCloseLayoutMetrics>({
    queryKey: metricsKey,
    enabled: enabled && managementEnabled,
    queryFn: async () => (await monthlyCloseApi.layoutMemoryMetrics()).data,
  });
  const createIntakeLink = useMutation({
    mutationFn: ({ label, sourceType }: { label: string; sourceType: string }) => monthlyCloseApi.createIntakeLink(month, { label, source_type: sourceType }),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: linksKey }); },
  });
  const revokeIntakeLink = useMutation({
    mutationFn: (linkId: string) => monthlyCloseApi.revokeIntakeLink(month, linkId),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: linksKey }); },
  });
  const setLayoutMemoryEnabled = useMutation({
    mutationFn: ({ documentId, value }: { documentId: string; value: boolean }) => monthlyCloseApi.setLayoutMemoryEnabled(documentId, value),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: memoriesKey }),
        queryClient.invalidateQueries({ queryKey: metricsKey }),
      ]);
    },
  });
  const receiveInbox = useMutation({
    mutationFn: (file: File) => workspaceApi.receiveInbox(month, file),
    onSuccess: (response, file) => {
      setDurableReceipts((current) => current.some((receipt) => receipt.receipt_id === response.data.receipt_id)
        ? current
        : [...current, {
            receipt_id: response.data.receipt_id,
            document_id: response.data.document_id,
            filename: file.name,
            storage_state: "stored",
            classification_state: "pending",
            processing_job_state: response.data.processing_status,
            analysis_state: "not_started",
          }]);
      void invalidate();
    },
  });
  const classifyInbox = useMutation({
    mutationFn: (itemId: string) => monthlyCloseApi.classifyInbox(month, itemId),
    onSuccess: async () => { await invalidate(); },
  });
  const setInboxSource = useMutation({
    mutationFn: ({ itemId, sourceType }: { itemId: string; sourceType: string }) => monthlyCloseApi.setInboxSource(month, itemId, sourceType),
    onSuccess: async () => { await invalidate(); },
  });
  const confirmInbox = useMutation({
    mutationFn: (itemId: string) => monthlyCloseApi.confirmInbox(month, itemId),
    onSuccess: async () => { await invalidate(); },
  });
  const permanentlyDeleteInbox = useMutation({
    mutationFn: (itemId: string) => monthlyCloseApi.deleteInbox(month, itemId),
    onSuccess: async (_response, itemId) => {
      setDurableReceipts((current) => current.filter(
        (receipt) => receipt.receipt_id !== itemId,
      ));
      queryClient.removeQueries({ queryKey: ["monthly-close-evidence"] });
      await invalidate();
    },
  });
  const confirmStep = useMutation({
    mutationFn: ({ stepKey, evidenceHash }: { stepKey: string; evidenceHash: string }) =>
      monthlyCloseApi.confirmStep(month, stepKey, evidenceHash),
    onSuccess: async (response) => {
      queryClient.setQueryData(queryKey, response.data);
      await queryClient.invalidateQueries({ queryKey: projectionKey, exact: true });
    },
  });
  const reopen = useMutation({
    mutationFn: (reason: string) => monthlyCloseApi.reopen(month, reason),
    onSuccess: (response) => {
      queryClient.setQueryData(queryKey, response.data);
      void queryClient.invalidateQueries({ queryKey: projectionKey, exact: true });
    },
  });
  const uploadDocument = useMutation({
    mutationFn: ({ sourceType, file }: { sourceType: string; file: File }) =>
      workspaceApi.uploadDocument(month, sourceType, file),
    onSuccess: (response, variables) => {
      const data = response.data as { receipt_id?: string; document_id?: string | null; processing_status?: "queued" | "processing" | "needs_review" | "completed" | "failed_safe" };
      if (data.receipt_id) {
        setDurableReceipts((current) => current.some((receipt) => receipt.receipt_id === data.receipt_id)
          ? current
          : [...current, {
              receipt_id: data.receipt_id!,
              document_id: data.document_id ?? null,
              filename: variables.file.name,
              storage_state: "stored",
              classification_state: "pending",
              processing_job_state: data.processing_status ?? "queued",
              analysis_state: "not_started",
            }]);
      } else if (data.document_id) {
        const analysisState = data.processing_status === "completed"
          ? "ready"
          : data.processing_status === "processing"
            ? "running"
            : data.processing_status === "needs_review"
              ? "needs_mapping"
              : data.processing_status === "failed_safe"
                ? "failed"
                : "queued";
        setDurableReceipts((current) => [...current.filter((receipt) => receipt.document_id !== data.document_id), {
          receipt_id: data.document_id!,
          document_id: data.document_id,
          filename: variables.file.name,
          storage_state: "stored",
          classification_state: "confirmed",
          analysis_state: analysisState,
        }]);
      }
      void invalidate();
    },
  });
  const markNotApplicable = useMutation({
    mutationFn: ({ sourceType, reason }: { sourceType: string; reason: string }) =>
      monthlyCloseApi.markNotApplicable(month, sourceType, reason),
    onSuccess: invalidate,
  });
  const archiveDocument = useMutation({
    mutationFn: (documentId: string) => monthlyCloseApi.archiveDocument(month, documentId),
    onSuccess: invalidate,
  });
  const runUtility = useMutation({
    mutationFn: () => monthlyCloseApi.runUtility(month),
    onSuccess: invalidate,
  });
  const importOperatingExpenses = useMutation({
    mutationFn: () => monthlyCloseApi.importOperatingExpenses(month),
    onSuccess: invalidate,
  });
  const reconcileServiceFees = useMutation({
    mutationFn: () => monthlyCloseApi.reconcileServiceFees(month),
    onSuccess: invalidate,
  });
  const generateSettlements = useMutation({
    mutationFn: () => {
      const [year, monthNumber] = month.split("-").map(Number);
      return settlementsApi.generate(year, monthNumber, true);
    },
    onSuccess: invalidate,
  });
  return {
    cycle,
    projection,
    sourceProposals,
    visibleProjection: scopedVisibleProjection,
    events,
    replies,
    durableReceipts,
    overview,
    inbox,
    intakeLinks,
    layoutMemories,
    layoutMetrics,
    start,
    confirmStep,
    reopen,
    receiveInbox,
    classifyInbox,
    setInboxSource,
    confirmInbox,
    permanentlyDeleteInbox,
    createIntakeLink,
    revokeIntakeLink,
    setLayoutMemoryEnabled,
    uploadDocument,
    markNotApplicable,
    archiveDocument,
    runUtility,
    importOperatingExpenses,
    reconcileServiceFees,
    generateSettlements,
    sendMessage,
    assistantProgress,
    stopMessage,
    loadEvidence,
    retryAnalysis,
    refresh: invalidate,
  };
}
