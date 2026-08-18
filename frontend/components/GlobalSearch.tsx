"use client";

import React, { useState } from "react";
import { Input, Modal, List, Tag, Typography, Space, Empty, Spin } from "antd";
import {
  SearchOutlined,
  FileTextOutlined,
  UserOutlined,
  HomeOutlined,
} from "@ant-design/icons";
import { useRouter } from "next/navigation";
import { searchApi } from "@/lib/api";
import type { SearchResult } from "@/lib/types";
import { orderStatusTagColor, ORDER_STATUS_LABEL } from "@/lib/status-display";

const { Text } = Typography;

// 房态徽标暂留本地（与中央 ROOM_STATUS_COLOR 色/文案有分叉，统一属另一项待王总拍板，
// 本批只收敛订单状态色）。
const ROOM_STATUS_LABELS: Record<string, { color: string; label: string }> = {
  available: { color: "green", label: "空闲" },
  occupied: { color: "blue", label: "在住" },
  reserved: { color: "cyan", label: "已预订" },
  pending_clean: { color: "orange", label: "待清扫" },
  cleaning: { color: "gold", label: "清扫中" },
  maintenance: { color: "default", label: "维修" },
  locked: { color: "red", label: "锁定" },
};

export default function GlobalSearch() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<SearchResult | null>(null);

  const handleSearch = async (value: string) => {
    if (!value.trim()) return;
    setOpen(true);
    setLoading(true);
    try {
      const res = await searchApi.search(value.trim());
      setResults(res.data);
    } catch {
      setResults(null);
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    setOpen(false);
    setResults(null);
  };

  const hasResults =
    results &&
    (results.orders.length > 0 ||
      results.guests.length > 0 ||
      results.rooms.length > 0);

  return (
    <>
      <Input.Search
        placeholder="搜索订单/客人/房间..."
        onSearch={handleSearch}
        style={{ width: 220 }}
        allowClear
        size="middle"
      />
      <Modal
        open={open}
        onCancel={handleClose}
        title={
          <Space>
            <SearchOutlined />
            <Text strong>搜索结果</Text>
          </Space>
        }
        footer={null}
        width={600}
      >
        {loading ? (
          <div style={{ textAlign: "center", padding: 40 }}>
            <Spin size="large" />
          </div>
        ) : !hasResults ? (
          <Empty description="未找到匹配结果" />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {results!.orders.length > 0 && (
              <div>
                <Text
                  type="secondary"
                  strong
                  style={{ fontSize: 13, marginBottom: 8, display: "block" }}
                >
                  <FileTextOutlined /> 订单 ({results!.orders.length})
                </Text>
                <List
                  size="small"
                  dataSource={results!.orders}
                  renderItem={(item) => {
                    const stLabel = ORDER_STATUS_LABEL[item.order_status];
                    return (
                      <List.Item
                        style={{ cursor: "pointer", padding: "8px 12px" }}
                        onClick={() => {
                          handleClose();
                          router.push("/orders");
                        }}
                      >
                        <Space
                          style={{
                            width: "100%",
                            justifyContent: "space-between",
                          }}
                        >
                          <Space>
                            <Text code style={{ fontSize: 11 }}>
                              {item.order_id}
                            </Text>
                            <Text>{item.guest_name}</Text>
                            <Text type="secondary" style={{ fontSize: 12 }}>
                              {item.check_in_date}
                            </Text>
                          </Space>
                          {stLabel && <Tag color={orderStatusTagColor(item.order_status)}>{stLabel}</Tag>}
                        </Space>
                      </List.Item>
                    );
                  }}
                />
              </div>
            )}

            {results!.guests.length > 0 && (
              <div>
                <Text
                  type="secondary"
                  strong
                  style={{ fontSize: 13, marginBottom: 8, display: "block" }}
                >
                  <UserOutlined /> 客人 ({results!.guests.length})
                </Text>
                <List
                  size="small"
                  dataSource={results!.guests}
                  renderItem={(item) => (
                    <List.Item
                      style={{ cursor: "pointer", padding: "8px 12px" }}
                      onClick={() => {
                        handleClose();
                        router.push("/guests");
                      }}
                    >
                      <Space>
                        <Text strong>{item.name}</Text>
                        <Text type="secondary">{item.phone}</Text>
                        <Tag bordered={false}>{item.visit_count} 次入住</Tag>
                      </Space>
                    </List.Item>
                  )}
                />
              </div>
            )}

            {results!.rooms.length > 0 && (
              <div>
                <Text
                  type="secondary"
                  strong
                  style={{ fontSize: 13, marginBottom: 8, display: "block" }}
                >
                  <HomeOutlined /> 房间 ({results!.rooms.length})
                </Text>
                <List
                  size="small"
                  dataSource={results!.rooms}
                  renderItem={(item) => {
                    const rs = ROOM_STATUS_LABELS[item.room_status];
                    return (
                      <List.Item
                        style={{ cursor: "pointer", padding: "8px 12px" }}
                        onClick={() => {
                          handleClose();
                          router.push("/rooms");
                        }}
                      >
                        <Space>
                          <Text strong>{item.room_id}</Text>
                          <Text>{item.room_name}</Text>
                          {rs && <Tag color={rs.color}>{rs.label}</Tag>}
                        </Space>
                      </List.Item>
                    );
                  }}
                />
              </div>
            )}
          </div>
        )}
      </Modal>
    </>
  );
}
