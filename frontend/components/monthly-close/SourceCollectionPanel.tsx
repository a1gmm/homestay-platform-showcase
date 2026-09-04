import { useState } from "react";
import { Button, Input, Popconfirm, Space, Upload, message } from "antd";
import { DeleteOutlined, DownloadOutlined, UploadOutlined } from "@ant-design/icons";
import type { UploadProps } from "antd";

import type { MonthlyCloseSource } from "@/lib/monthly-close";
import { MONTHLY_CLOSE_SOURCE_LABELS } from "@/lib/monthly-close";
import { monthlyCloseApi } from "@/lib/api";
import { downloadBlob } from "@/lib/utils";
import { tokens } from "@/lib/design-tokens";
import { ServiceMappingReview } from "./ServiceMappingReview";

export function SourceCollectionPanel({
  sources,
  uploading,
  billingMonth,
  readOnly,
  onRefresh,
  onUpload,
  onNotApplicable,
  onArchive,
  uploadLabel = "上传文件",
  showUploadActions = true,
}: {
  sources: MonthlyCloseSource[];
  billingMonth: string;
  uploading: boolean;
  readOnly?: boolean;
  onRefresh: () => Promise<unknown>;
  onUpload: (sourceType: string, file: File) => Promise<unknown>;
  onNotApplicable: (sourceType: string, reason: string) => Promise<unknown>;
  onArchive: (documentId: string) => Promise<unknown>;
  uploadLabel?: string;
  showUploadActions?: boolean;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  const requestFor = (sourceType: string): NonNullable<UploadProps["customRequest"]> =>
    async ({ file, onSuccess, onError }) => {
      try {
        await onUpload(sourceType, file as File);
        onSuccess?.({});
        message.success("资料已归档");
      } catch (error) {
        onError?.(error as Error);
        message.error("上传失败，请检查文件后重试");
      }
    };

  const download = async (documentId: string, filename: string) => {
    const response = await monthlyCloseApi.downloadDocument(documentId);
    downloadBlob(response.data, filename);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div
        role="note"
        style={{
          borderLeft: `2px solid ${tokens.anyu.color.sage}`,
          paddingLeft: 12,
          color: tokens.color.text.secondary,
          lineHeight: 1.7,
        }}
      >
        直接上传现有 Excel 原表，无需套模板。系统会自动识别工作表、表头和字段；只有不确定的地方才需要管理员确认。
      </div>
      {sources.map((source) => (
        <section
          key={source.source_type}
          style={{
            border: `0.5px solid ${tokens.anyu.color.linen}`,
            borderRadius: tokens.anyu.radius.md,
            padding: 16,
            background: tokens.anyu.color.shell,
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
            <div style={{ minWidth: 180 }}>
              <div style={{ color: tokens.color.text.primary }}>{MONTHLY_CLOSE_SOURCE_LABELS[source.source_type] ?? source.source_type}</div>
              <div style={{ color: source.state === "pending" ? tokens.anyu.color.clay : tokens.anyu.color.sage, fontSize: 12, marginTop: 4 }}>
                {source.state === "uploaded" ? "已上传" : source.state === "not_applicable" ? `不适用 · ${source.not_applicable_reason}` : "等待资料"}
              </div>
            </div>
            <Space wrap>
              {showUploadActions && (
                <Upload disabled={readOnly} accept=".xls,.xlsx" showUploadList={false} customRequest={requestFor(source.source_type)}>
                  <Button aria-label={uploadLabel} disabled={readOnly} icon={<UploadOutlined />} loading={uploading}>{uploadLabel}</Button>
                </Upload>
              )}
              {!readOnly && source.documents.length === 0 && source.state !== "not_applicable" && (
                <Button onClick={() => { setEditing(source.source_type); setReason(""); }}>本月不适用</Button>
              )}
            </Space>
          </div>
          {editing === source.source_type && (
            <Space.Compact style={{ width: "100%", marginTop: 12 }}>
              <Input aria-label={`${MONTHLY_CLOSE_SOURCE_LABELS[source.source_type]}不适用原因`} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="说明本月为什么没有这类资料" />
              <Button
                disabled={!reason.trim()}
                onClick={async () => {
                  await onNotApplicable(source.source_type, reason.trim());
                  setEditing(null);
                  setReason("");
                }}
              >确认不适用</Button>
            </Space.Compact>
          )}
          {source.documents.map((document) => (
            <div key={document.document_id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, paddingTop: 12, marginTop: 12, borderTop: `0.5px solid ${tokens.anyu.color.linen}` }}>
              <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {document.filename}
                <span style={{ color: tokens.color.text.tertiary, marginLeft: 8, fontSize: 12 }}>
                  {document.processing_status === "processed"
                    ? "已处理"
                    : document.processing_status === "rejected"
                      ? document.processing_error || "处理失败，需替换"
                      : "已归档"}
                </span>
              </span>
              <Space>
                {document.processing_status === "rejected" && (source.source_type === "cleaning_statement" || source.source_type === "linen_statement") && (
                  <ServiceMappingReview billingMonth={billingMonth} document={document} disabled={readOnly} onFinished={onRefresh} />
                )}
                <Button aria-label={`下载${document.filename}`} type="text" icon={<DownloadOutlined />} onClick={() => void download(document.document_id, document.filename)} />
                <Popconfirm disabled={readOnly} title="归档后本文件不再参与当前月结，确认继续？" onConfirm={() => onArchive(document.document_id)}>
                  <Button disabled={readOnly} aria-label={`归档${document.filename}`} type="text" icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}
