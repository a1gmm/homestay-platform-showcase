import { useRef, useState } from "react";
import { Alert, Button, message } from "antd";

import { MappingReviewDrawer } from "@/components/billing-recon/MappingReviewDrawer";
import { monthlyCloseApi } from "@/lib/api";
import type { BillingReconConfirmInput, BillingReconFieldError, MappingCoordinates, WorkbookAnalysis } from "@/lib/billing-recon";
import type { MonthlyCloseDocument } from "@/lib/monthly-close";
import { extractErrorMessage } from "@/lib/api-errors";

function mappingVersion(mapping: MappingCoordinates) {
  return JSON.stringify(mapping);
}

export function OtaStatementPanel({
  billingMonth,
  documents,
  disabled,
  onFinished,
}: {
  billingMonth: string;
  documents: MonthlyCloseDocument[];
  disabled?: boolean;
  onFinished: () => Promise<unknown>;
}) {
  const target = documents.find((document) => !document.engine_id) ?? null;
  const [analysis, setAnalysis] = useState<WorkbookAnalysis | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<BillingReconFieldError | null>(null);
  const [currentVersion, setCurrentVersion] = useState<string | null>(null);
  const [verifiedVersion, setVerifiedVersion] = useState<string | null>(null);
  const requestRef = useRef(0);

  const analyze = async (coordinates?: MappingCoordinates) => {
    if (!target) return;
    const request = requestRef.current + 1;
    requestRef.current = request;
    setLoading(true);
    setError(null);
    try {
      const response = await monthlyCloseApi.analyzeOta(billingMonth, target.document_id, coordinates);
      if (request !== requestRef.current) return;
      setAnalysis(response.data);
      const version = mappingVersion(response.data.coordinates);
      setCurrentVersion(version);
      setVerifiedVersion(version);
      setOpen(true);
    } catch (cause) {
      if (request !== requestRef.current) return;
      setError({ code: "AI_UNAVAILABLE", message: extractErrorMessage(cause, "账单分析失败，请稍后重试"), field: "file" });
      setOpen(true);
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  };

  if (!target) {
    return documents.length > 0
      ? <Alert type="info" showIcon message="所有已上传OTA账单均已生成对账批次" />
      : null;
  }
  return (
    <>
      <Button disabled={disabled} loading={loading} onClick={() => void analyze()}>
        分析并核对 {target.filename} →
      </Button>
      <MappingReviewDrawer
        open={open}
        analysis={analysis}
        analysisCurrent={currentVersion !== null && currentVersion === verifiedVersion}
        loading={loading}
        error={error}
        onClose={() => setOpen(false)}
        onMappingChange={(coordinates) => setCurrentVersion(mappingVersion(coordinates))}
        onReanalyze={(coordinates) => void analyze(coordinates)}
        onConfirm={async (input: Omit<BillingReconConfirmInput, "file" | "file_fingerprint">) => {
          if (!target) return;
          setLoading(true);
          setError(null);
          try {
            const response = await monthlyCloseApi.confirmOta(billingMonth, target.document_id, input);
            message.success(`已生成 ${response.data.batch.bill_month} OTA对账批次`);
            setOpen(false);
            await onFinished();
          } catch (cause) {
            setError({ code: "MAPPING_INCOMPLETE", message: extractErrorMessage(cause, "确认对账失败"), field: "mapping_json" });
          } finally {
            setLoading(false);
          }
        }}
      />
    </>
  );
}
