import { useState } from "react";
import { Alert, Button, Input, Space, Tag, message } from "antd";

import type { MonthlyCloseIntakeLink, MonthlyCloseIntakeLinkCreated } from "@/lib/monthly-close";
import { MONTHLY_CLOSE_SOURCE_LABELS } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";
import { extractErrorMessage } from "@/lib/api-errors";

export function IntakeLinkManager({ links, onCreate, onRevoke }: {
  links: MonthlyCloseIntakeLink[];
  onCreate: (label: string, sourceType: string) => Promise<MonthlyCloseIntakeLinkCreated>;
  onRevoke: (linkId: string) => Promise<unknown>;
}) {
  const [createdUrl, setCreatedUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const create = async (sourceType: string, label: string) => {
    setBusy(sourceType);
    setLocalError(null);
    try {
      const created = await onCreate(label, sourceType);
      setCreatedUrl(`${window.location.origin}/monthly-close-upload/${created.token}`);
      message.success("收件链接已生成");
    } catch (error) {
      setLocalError(extractErrorMessage(error, "收件链接生成失败，请重试"));
    } finally { setBusy(null); }
  };

  const revoke = async (linkId: string) => {
    setBusy(linkId);
    setLocalError(null);
    try {
      await onRevoke(linkId);
    } catch (error) {
      setLocalError(extractErrorMessage(error, "收件链接停用失败，请重试"));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <Alert type="info" showIcon message="链接只能上传文件" description="供应商看不到订单、金额、业主或其他资料。链接到期或停用后立即失效。" />
      {localError && <Alert type="error" showIcon message={localError} />}
      {createdUrl && (
        <section aria-label="新生成的收件链接" style={{ border: `0.5px solid ${tokens.anyu.color.linen}`, borderRadius: 10, padding: 14 }}>
          <div style={{ marginBottom: 8 }}>该地址只显示这一次，请现在发给供应商：</div>
          <Space.Compact style={{ width: "100%" }}>
            <Input readOnly value={createdUrl} />
            <Button onClick={async () => {
              if (!navigator.clipboard) {
                message.info("请长按或选中地址手动复制");
                return;
              }
              try {
                await navigator.clipboard.writeText(createdUrl);
                message.success("已复制");
              } catch {
                message.info("复制失败，请选中地址手动复制");
              }
            }}>复制</Button>
          </Space.Compact>
        </section>
      )}
      <section aria-label="生成供应商收件链接">
        <div className="serif" style={{ fontSize: 18, marginBottom: 10 }}>按资料类型生成链接</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))", gap: 9 }}>
          {Object.entries(MONTHLY_CLOSE_SOURCE_LABELS).map(([sourceType, label]) => (
            <Button key={sourceType} aria-label={`生成${label}收件链接`} loading={busy === sourceType} disabled={busy !== null} onClick={() => void create(sourceType, label)}>{label} →</Button>
          ))}
        </div>
      </section>
      <section aria-label="已有收件链接">
        <div className="serif" style={{ fontSize: 18, marginBottom: 10 }}>本月已有链接</div>
        {links.length === 0 ? <div style={{ color: tokens.color.text.tertiary }}>尚未生成</div> : links.map((link) => {
          const inactive = !!link.revoked_at || new Date(link.expires_at).getTime() <= Date.now();
          return (
            <div key={link.link_id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, padding: "10px 0", borderTop: `0.5px solid ${tokens.anyu.color.linen}` }}>
              <div><div>{link.label}</div><div style={{ color: tokens.color.text.tertiary, fontSize: 12, marginTop: 3 }}>{link.last_uploaded_at ? "已有文件提交" : "等待供应商提交"}</div></div>
              <Space><Tag color={inactive ? "default" : "success"}>{inactive ? "已失效" : "有效"}</Tag>{!inactive && <Button danger loading={busy === link.link_id} disabled={busy !== null} aria-label={`停用${link.label}收件链接`} onClick={() => void revoke(link.link_id)}>停用</Button>}</Space>
            </div>
          );
        })}
      </section>
    </div>
  );
}
