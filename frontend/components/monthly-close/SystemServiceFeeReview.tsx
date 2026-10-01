"use client";

import { useState } from "react";
import { Alert, Button, message } from "antd";

import { monthlyCloseApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import { SourceProposalCard } from "./SourceProposalCard";
import { SourceProposalHistory } from "./SourceProposalHistory";
import { sourceRequestId } from "./sourceRequestId";
import { useSourceProposalRecovery } from "./useSourceProposalRecovery";

export function SystemServiceFeeReview({ billingMonth, role, onFinished }: {
  billingMonth: string;
  role: string;
  onFinished: () => Promise<unknown>;
}) {
  const [proposal, setProposal, items, reload, sourceState] = useSourceProposalRecovery(
    billingMonth,
    "service_fee_reconciliation",
  );
  const [creating, setCreating] = useState(false);

  const create = async () => {
    setCreating(true);
    try {
      const nextVersion = Math.max(0, ...items.map((item) => item.proposal_version ?? 0)) + 1;
      const response = await monthlyCloseApi.createServiceProposal(
        billingMonth,
        sourceRequestId("system-service-fee", billingMonth, "system-derived", {
          submission_version: nextVersion,
          issue_codes: sourceState?.issue_codes ?? [],
          source_state: sourceState?.state ?? "ready",
        }),
      );
      setProposal(response.data);
      await onFinished();
      message.success("系统服务费方案已生成，等待批准");
    } catch (error) {
      message.error(extractErrorMessage(error, "系统服务费方案生成失败"));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <Alert
        type="info"
        showIcon
        message="系统服务费来自已完成订单与当前费率规则"
        description="无需上传服务商表格；系统只生成确定性方案，仍需管理员批准、执行并复核。"
      />
      <SourceProposalHistory items={items} />
      {proposal ? (
        <SourceProposalCard
          billingMonth={billingMonth}
          proposal={proposal}
          role={role}
          onProposal={setProposal}
          onFinished={async () => {
            await reload();
            await onFinished();
          }}
        />
      ) : sourceState?.state === "completed" ? (
        <Alert type="success" showIcon message="当前系统服务费已对完，无需变更" />
      ) : (
        <Button
          type="primary"
          loading={creating}
          disabled={sourceState?.state !== "ready" || !["admin", "finance", "operator"].includes(role)}
          onClick={() => void create()}
        >
          生成系统服务费方案
        </Button>
      )}
    </div>
  );
}
