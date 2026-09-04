"use client";

import { useEffect, useMemo, useState } from "react";
import dayjs from "dayjs";
import { Alert, Button, Drawer, Empty, Input, Modal, Skeleton, message } from "antd";
import { useRouter } from "next/navigation";

import { PageHeader } from "@/components/ui/PageHeader";
import { CloseProgress } from "@/components/monthly-close/CloseProgress";
import { CurrentStepPanel } from "@/components/monthly-close/CurrentStepPanel";
import { IntakeLinkManager } from "@/components/monthly-close/IntakeLinkManager";
import { LayoutMemoryManager } from "@/components/monthly-close/LayoutMemoryManager";
import { MonthlyCloseControlTower } from "@/components/monthly-close/MonthlyCloseControlTower";
import { useAuthStore } from "@/lib/auth";
import { useMonthlyClose } from "@/hooks/useMonthlyClose";
import { extractErrorMessage } from "@/lib/api-errors";
import { tokens } from "@/lib/design-tokens";
import type { MonthlyCloseCycle, MonthlyCloseStep } from "@/lib/monthly-close";

function monthStatusText(
  month: string,
  cycle: MonthlyCloseCycle | null | undefined,
  step: MonthlyCloseStep | undefined,
) {
  const monthLabel = dayjs(`${month}-01`).format("YYYY年MM月");
  if (cycle === undefined) return `${monthLabel} · 正在读取`;
  if (cycle === null) return `${monthLabel} · 尚未开始`;
  if (cycle.status === "completed") return `${monthLabel} · 已完成`;
  if (cycle.status === "needs_recheck") return `${monthLabel} · 数据有变化，需重新核对`;
  if (!step) return `${monthLabel} · 已完成`;
  if (step.blocking_count > 0) {
    const pending = step.step_key === "source_collection"
      ? `还缺 ${step.blocking_count} 项`
      : `待处理 ${step.blocking_count} 项`;
    return `${monthLabel} · ${step.label} · ${pending}`;
  }
  return `${monthLabel} · ${step.label} · 可以确认`;
}

export default function MonthlyClosePage() {
  const router = useRouter();
  const { user } = useAuthStore();
  const isAdmin = !!user && user.role === "admin";
  const [month, setMonth] = useState(() => {
    if (typeof window !== "undefined") {
      const requested = new URLSearchParams(window.location.search).get("month");
      if (requested && /^\d{4}-(0[1-9]|1[0-2])$/.test(requested)) return requested;
    }
    return dayjs().subtract(1, "month").format("YYYY-MM");
  });
  const [reopenOpen, setReopenOpen] = useState(false);
  const [reopenReason, setReopenReason] = useState("");
  const [managementOpen, setManagementOpen] = useState(false);
  const [selectedStepKey, setSelectedStepKey] = useState<string | null>(null);
  const { cycle, overview, inbox, intakeLinks, layoutMemories, layoutMetrics, start, confirmStep, reopen, receiveInbox, classifyInbox, setInboxSource, confirmInbox, createIntakeLink, revokeIntakeLink, setLayoutMemoryEnabled, uploadDocument, markNotApplicable, archiveDocument, runUtility, importOperatingExpenses, reconcileServiceFees, generateSettlements, refresh } = useMonthlyClose(month, isAdmin, managementOpen);

  const selectMonth = (next: string) => {
    setSelectedStepKey(null);
    setMonth(next);
    window.history.replaceState(null, "", `/finance/monthly-close?month=${next}`);
  };

  useEffect(() => {
    if (user && !isAdmin) router.replace("/dashboard");
  }, [isAdmin, router, user]);

  const workflowStep = useMemo(() => {
    const data = cycle.data;
    if (!data) return undefined;
    return data.steps.find((step) => step.step_key === data.current_step)
      ?? data.steps.find((step) => step.status !== "confirmed");
  }, [cycle.data]);
  const displayedStep = useMemo(() => {
    const data = cycle.data;
    if (!data) return undefined;
    return data.steps.find((step) => step.step_key === selectedStepKey) ?? workflowStep;
  }, [cycle.data, selectedStepKey, workflowStep]);

  if (!isAdmin) return null;
  return (
    <div className="monthly-close-page" style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="月结中心"
        subtitle={monthStatusText(month, cycle.data, workflowStep)}
        extra={(
          <div className="monthly-close-toolbar" style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
            <Button onClick={() => router.push("/settlements")}>历史结算</Button>
            <Button onClick={() => setManagementOpen(true)}>月结管理</Button>
            <label className="monthly-close-month-control" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ color: tokens.color.text.secondary }}>月份</span>
              <input
                aria-label="月结月份"
                type="month"
                value={month}
                onChange={(event) => selectMonth(event.target.value)}
                style={{ minHeight: 40, border: `1px solid ${tokens.anyu.color.linen}`, borderRadius: 8, background: tokens.anyu.color.shell, padding: "0 12px", font: "inherit" }}
              />
            </label>
          </div>
        )}
      />

      {cycle.isLoading ? <Skeleton active paragraph={{ rows: 8 }} /> : cycle.isError || cycle.data === undefined ? (
        <Alert
          type="error"
          showIcon
          message="月结数据加载失败"
          description={extractErrorMessage(cycle.error, "请检查网络后重试")}
          action={<Button onClick={() => cycle.refetch()}>重新加载</Button>}
        />
      ) : cycle.data === null ? (
        <section style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, background: tokens.anyu.color.shell, padding: "40px 24px" }}>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={(
              <div>
                <div style={{ color: tokens.color.text.primary, fontSize: 16, fontWeight: 500 }}>该月份尚未开始月结</div>
                <div style={{ color: tokens.color.text.secondary, marginTop: 6 }}>你可以先查看该月已有的历史结算，或明确开始新的九步月结流程。</div>
              </div>
            )}
          >
            <div style={{ display: "flex", justifyContent: "center", flexWrap: "wrap", gap: 12 }}>
              <Button onClick={() => router.push(`/settlements?month=${month}`)}>查看该月历史结算</Button>
              <Button
                type="primary"
                loading={start.isPending}
                onClick={async () => {
                  try {
                    await start.mutateAsync();
                    message.success("已开始该月月结");
                  } catch (error) {
                    message.error(extractErrorMessage(error, "开始月结失败"));
                  }
                }}
              >
                开始该月月结
              </Button>
            </div>
          </Empty>
        </section>
      ) : (
        <>
          {cycle.data.stored_status === "completed" && (
            <Alert
              type={cycle.data.status === "needs_recheck" ? "warning" : "info"}
              showIcon
              message={cycle.data.status === "needs_recheck" ? "完成后的业务数据发生变化" : "该月份已完成，当前为只读状态"}
              description="如需重新处理，必须先填写原因重新打开；原完成快照和确认历史会保留。"
              action={<Button onClick={() => setReopenOpen(true)}>填写原因后重新打开</Button>}
            />
          )}

          <div className="monthly-close-workspace" style={{ display: "grid", gridTemplateColumns: "minmax(260px, 340px) minmax(0, 1fr)", gap: 24, alignItems: "start" }}>
            <aside className="monthly-close-steps-shell" style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, overflow: "hidden" }}>
              <CloseProgress
                steps={cycle.data.steps}
                currentStepKey={cycle.data.current_step}
                selectedStepKey={displayedStep?.step_key}
                onSelect={setSelectedStepKey}
              />
            </aside>
            <main className="monthly-close-current-panel" style={{ minWidth: 0, border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 12, padding: 24 }}>
              <CurrentStepPanel
                cycle={cycle.data}
                step={displayedStep}
                confirming={confirmStep.isPending}
                uploading={uploadDocument.isPending || receiveInbox.isPending || classifyInbox.isPending || setInboxSource.isPending || confirmInbox.isPending}
                engineRunning={runUtility.isPending || importOperatingExpenses.isPending || reconcileServiceFees.isPending || generateSettlements.isPending}
                readOnly={cycle.data.stored_status === "completed"}
                inboxItems={inbox.data ?? []}
                onSelectStep={setSelectedStepKey}
                onConfirm={async () => {
                  if (!displayedStep) return;
                  try {
                    await confirmStep.mutateAsync({ stepKey: displayedStep.step_key, evidenceHash: displayedStep.evidence_hash });
                    setSelectedStepKey(null);
                    message.success("本步骤已确认，已进入下一步");
                  } catch (error) {
                    message.error(extractErrorMessage(error, "确认失败，数据可能已变化，请刷新后重试"));
                    await cycle.refetch();
                  }
                }}
                onReceiveInbox={async (file) => (await receiveInbox.mutateAsync(file)).data}
                onClassifyInbox={async (itemId) => { await classifyInbox.mutateAsync(itemId); }}
                onSetInboxSource={async (itemId, sourceType) => { await setInboxSource.mutateAsync({ itemId, sourceType }); }}
                onConfirmInbox={async (itemId) => { await confirmInbox.mutateAsync(itemId); }}
                onUpload={(sourceType, file) => uploadDocument.mutateAsync({ sourceType, file })}
                onNotApplicable={(sourceType, reason) => markNotApplicable.mutateAsync({ sourceType, reason })}
                onArchive={(documentId) => archiveDocument.mutateAsync(documentId)}
                onRunUtility={async () => {
                  try {
                    await runUtility.mutateAsync();
                    message.success("已用归档原件生成水电对账批次");
                  } catch (error) {
                    message.error(extractErrorMessage(error, "水电对账启动失败"));
                  }
                }}
                onImportOperatingExpenses={async () => {
                  try {
                    const response = await importOperatingExpenses.mutateAsync();
                    if (response.data.failed.length > 0) {
                      message.warning(`已导入 ${response.data.imported_count} 条，另有 ${response.data.failed.length} 条需要修正`);
                    } else {
                      message.success(`已导入 ${response.data.imported_count} 条运营支出`);
                    }
                  } catch (error) {
                    message.error(extractErrorMessage(error, "运营支出导入失败"));
                  }
                }}
                onReconcileServiceFees={async () => {
                  try {
                    const response = await reconcileServiceFees.mutateAsync();
                    if (response.data.blocked.length > 0) {
                      message.warning(`已完成自动核对，仍有 ${response.data.blocked.length} 项需人工处理`);
                    } else {
                      message.success(`已补齐 ${response.data.created_count} 条、修正 ${response.data.corrected_count} 条服务费`);
                    }
                  } catch (error) {
                    message.error(extractErrorMessage(error, "服务费核对失败"));
                  }
                }}
                onGenerateSettlements={async () => {
                  try {
                    const response = await generateSettlements.mutateAsync();
                    message.success(`已生成 ${response.data.generated} 份、重新生成 ${response.data.regenerated} 份结算单`);
                  } catch (error) {
                    message.error(extractErrorMessage(error, "结算单生成失败"));
                  }
                }}
                onRefresh={() => refresh()}
              />
            </main>
          </div>
          <Modal
            title="重新打开本月月结"
            open={reopenOpen}
            okText="重新打开"
            cancelText="取消"
            confirmLoading={reopen.isPending}
            okButtonProps={{ disabled: !reopenReason.trim() }}
            onCancel={() => { setReopenOpen(false); setReopenReason(""); }}
            onOk={async () => {
              try {
                await reopen.mutateAsync(reopenReason.trim());
                message.success("本月月结已重新打开，原确认历史已保留");
                setReopenOpen(false);
                setReopenReason("");
              } catch (error) {
                message.error(extractErrorMessage(error, "重新打开失败"));
              }
            }}
          >
            <Input.TextArea
              aria-label="重新打开原因"
              rows={4}
              maxLength={1000}
              value={reopenReason}
              onChange={(event) => setReopenReason(event.target.value)}
              placeholder="说明为什么需要修改已完成月结"
            />
          </Modal>
        </>
      )}
      <Drawer
        title="月结管理"
        width={760}
        open={managementOpen}
        destroyOnHidden
        onClose={() => setManagementOpen(false)}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 32 }}>
          {overview.isError ? (
            <Alert type="warning" showIcon message="月份总览暂时加载失败" action={<Button onClick={() => overview.refetch()}>重试</Button>} />
          ) : (
            <MonthlyCloseControlTower rows={overview.data ?? []} selectedMonth={month} onSelect={(next) => { selectMonth(next); setManagementOpen(false); }} />
          )}
          <section aria-label="供应商收件链接管理">
            <div className="serif" style={{ fontSize: 20, marginBottom: 10 }}>供应商收件链接</div>
            {!cycle.data || cycle.data.stored_status === "completed" ? (
              <Alert type="info" showIcon message="当前月份不能新建收件链接" description="请先开始或重新打开该月月结，再生成供应商上传链接。" />
            ) : (
              <IntakeLinkManager
                links={intakeLinks.data ?? []}
                onCreate={async (label, sourceType) => (await createIntakeLink.mutateAsync({ label, sourceType })).data}
                onRevoke={async (linkId) => { await revokeIntakeLink.mutateAsync(linkId); }}
              />
            )}
          </section>
          <section aria-label="表格布局记忆管理">
            <div className="serif" style={{ fontSize: 20, marginBottom: 10 }}>表格布局记忆</div>
            <LayoutMemoryManager
              memories={layoutMemories.data ?? []}
              metrics={layoutMetrics.data ?? { memory_count: 0, file_count: 0, deterministic_recognition_count: 0, ai_suggestion_count: 0, remembered_layout_count: 0, administrator_correction_count: 0, needs_confirmation_count: 0, active_memory_count: 0, disabled_memory_count: 0, reuse_count: 0, automation_rate: 0 }}
              onSetEnabled={async (documentId, value) => { await setLayoutMemoryEnabled.mutateAsync({ documentId, value }); }}
            />
          </section>
        </div>
      </Drawer>
    </div>
  );
}
