import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { monthlyCloseApi, settlementsApi } from "@/lib/api";
import type { MonthlyCloseCycle, MonthlyCloseInboxItem, MonthlyCloseIntakeLink, MonthlyCloseLayoutMemory, MonthlyCloseLayoutMetrics, MonthlyCloseOverview } from "@/lib/monthly-close";

function isNotFound(error: unknown): boolean {
  return (error as { response?: { status?: number } } | null)?.response?.status === 404;
}

export function useMonthlyClose(month: string, enabled: boolean, managementEnabled = false) {
  const queryClient = useQueryClient();
  const queryKey = ["monthly-close", month] as const;
  const inboxKey = ["monthly-close-inbox", month] as const;
  const overviewKey = ["monthly-close-overview"] as const;
  const linksKey = ["monthly-close-intake-links", month] as const;
  const memoriesKey = ["monthly-close-layout-memories"] as const;
  const metricsKey = ["monthly-close-layout-memory-metrics"] as const;
  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey }),
      queryClient.invalidateQueries({ queryKey: inboxKey }),
      queryClient.invalidateQueries({ queryKey: overviewKey }),
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
    enabled: enabled && !!month,
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
    mutationFn: (file: File) => monthlyCloseApi.receiveInbox(month, file),
    onSuccess: async () => { await invalidate(); },
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
  const confirmStep = useMutation({
    mutationFn: ({ stepKey, evidenceHash }: { stepKey: string; evidenceHash: string }) =>
      monthlyCloseApi.confirmStep(month, stepKey, evidenceHash),
    onSuccess: (response) => queryClient.setQueryData(queryKey, response.data),
  });
  const reopen = useMutation({
    mutationFn: (reason: string) => monthlyCloseApi.reopen(month, reason),
    onSuccess: (response) => queryClient.setQueryData(queryKey, response.data),
  });
  const uploadDocument = useMutation({
    mutationFn: ({ sourceType, file }: { sourceType: string; file: File }) =>
      monthlyCloseApi.uploadDocument(month, sourceType, file),
    onSuccess: invalidate,
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
    refresh: invalidate,
  };
}
