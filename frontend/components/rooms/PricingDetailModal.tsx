"use client";

import { Modal, Typography, Button } from "antd";
import type { PricingDetail } from "./types";

const { Text } = Typography;

interface PricingDetailModalProps {
  open: boolean;
  onClose: () => void;
  roomId?: string;
  date?: string;
  detail?: PricingDetail;
}

export function PricingDetailModal({ open, onClose, roomId, date, detail }: PricingDetailModalProps) {
  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      title={<Text strong>价格溯源 — {roomId} — {date ?? ""}</Text>}
    >
      {detail ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>推荐价</Text>
            <div>
              <Text style={{ fontSize: 18, fontWeight: 600 }}>
                ¥{detail.recommended_price.toFixed(2)}
              </Text>
              {detail.base_price != null && (
                <Text style={{ marginLeft: 8, fontSize: 12 }}>
                  （基础价 ¥{detail.base_price.toFixed(2)}）
                </Text>
              )}
            </div>
          </div>
          {detail.competitor_avg_price != null && (
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>竞品参考均价（裁剪后）</Text>
              <div><Text>¥{detail.competitor_avg_price.toFixed(2)}</Text></div>
            </div>
          )}
          <div>
            <Text strong style={{ fontSize: 13 }}>参与计算的竞品样本（按价格从低到高）</Text>
            {detail.competitors?.length ? (
              <div style={{ marginTop: 6 }}>
                {detail.competitors.map((c, idx) => (
                  <div
                    key={`${c.name}-${idx}`}
                    style={{ display: "flex", justifyContent: "space-between", fontSize: 12, padding: "2px 0" }}
                  >
                    <Text style={{ maxWidth: 220 }} ellipsis>{c.name || "未命名酒店"}</Text>
                    <Text>¥{c.price.toFixed(2)}</Text>
                  </div>
                ))}
              </div>
            ) : (
              <Text type="secondary" style={{ fontSize: 12 }}>
                本次未获取到足够的竞品样本，仅基于规则引擎定价。
              </Text>
            )}
          </div>
          {detail.source_url && (
            <div style={{ marginTop: 8 }}>
              <Button type="link" href={detail.source_url} target="_blank" rel="noopener noreferrer">
                在携程打开当日搜索结果页
              </Button>
            </div>
          )}
        </div>
      ) : (
        <Text type="secondary" style={{ fontSize: 12 }}>正在加载定价详情…</Text>
      )}
    </Modal>
  );
}
