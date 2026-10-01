"use client";

import { useEffect, useState } from "react";
import { Alert, Button, Result, Skeleton } from "antd";
import { useMutation, useQuery } from "@tanstack/react-query";

import { monthlyCloseApi } from "@/lib/api";
import { tokens } from "@/lib/design-tokens";

export default function PublicMonthlyCloseUploadPage({ params }: { params: Promise<{ token: string }> }) {
  const [token, setToken] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [receipt, setReceipt] = useState<{ receipt_code: string; message: string } | null>(null);
  useEffect(() => { void params.then((value) => setToken(value.token)); }, [params]);
  const intake = useQuery({
    queryKey: ["monthly-close-public-intake", token],
    enabled: !!token,
    retry: false,
    queryFn: async () => (await monthlyCloseApi.getPublicIntake(token)).data,
  });
  const upload = useMutation({
    mutationFn: async (selected: File) => (await monthlyCloseApi.uploadPublicIntake(token, selected)).data,
    onSuccess: (value) => setReceipt(value),
  });

  return (
    <main style={{ minHeight: "100dvh", background: tokens.color.bg.page, padding: "clamp(24px, 7vw, 80px) 18px" }}>
      <section style={{ maxWidth: 620, margin: "0 auto", background: tokens.color.bg.container, border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 14, padding: "clamp(22px, 5vw, 42px)" }}>
        <div className="en-label">CHENGJIA · SECURE INTAKE</div>
        {intake.isLoading || !token ? <Skeleton active paragraph={{ rows: 5 }} /> : intake.isError ? (
          <Result status="warning" title="这个收件链接现在不能使用" subTitle="请联系发给你链接的工作人员，获取新的上传链接。" />
        ) : receipt ? (
          <Result status="success" title="文件已收到" subTitle={<><div>{receipt.message}</div><div style={{ marginTop: 8 }}>收件编号：{receipt.receipt_code}</div></>} />
        ) : (
          <>
            <h1 className="serif" style={{ fontSize: 30, fontWeight: 400, margin: "12px 0 8px" }}>上传{intake.data?.source_label || "月结资料"}</h1>
            <p style={{ color: tokens.color.text.secondary, lineHeight: 1.8, marginBottom: 22 }}>{intake.data?.label} · {intake.data?.billing_month}。请直接上传现有 Excel 原表，不用改格式。</p>
            <label style={{ display: "block", border: `1px dashed ${tokens.anyu.color.sage}`, borderRadius: 12, padding: 24, cursor: "pointer", textAlign: "center" }}>
              <span>{file ? file.name : "选择 xls / xlsx 文件（最大 10MB）"}</span>
              <input aria-label="选择月结资料" type="file" accept=".xls,.xlsx" style={{ display: "none" }} onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
            </label>
            {upload.isError && <Alert style={{ marginTop: 14 }} type="error" showIcon message="上传没有成功。请确认文件是 xls 或 xlsx、大小不超过 10MB，然后重试。" />}
            <Button block type="primary" size="large" style={{ marginTop: 18 }} disabled={!file} loading={upload.isPending} onClick={() => file && upload.mutate(file)}>提交文件</Button>
            <div style={{ color: tokens.color.text.tertiary, fontSize: 12, marginTop: 14, textAlign: "center" }}>此页面只能提交资料，不会显示任何内部业务数据。</div>
          </>
        )}
      </section>
    </main>
  );
}
