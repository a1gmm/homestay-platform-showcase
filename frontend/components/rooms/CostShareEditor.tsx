"use client";

/**
 * v2-issue#3 — 房间业主费用支出占比编辑器
 *
 * 使用场景：嵌入房间编辑 Modal 的「费用占比」Tab。参考王总参考 PMS 截图 4。
 * 三个 Tab（普通/试住/自住）× 14 行（费用类目）× (占比% + 单价¥) 输入。
 * 每个 Tab 顶部「批量更改」按钮 → 弹 Modal 一次性应用所有 14 类目。
 *
 * 数据流：
 * - useQuery 拉 GET /rooms/{roomId}/cost-share-rules → 转 (booking_type, category) → rule map
 * - InputNumber 改值 → 失焦时调 PUT upsert（仅传被改的那条）
 * - 批量按钮 → 弹 Modal → 一次性 PUT 14 条覆盖
 */
import { useMemo, useState } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Tabs, Table, InputNumber, Button, Modal, message, Space, Typography } from "antd";
import { roomsApi } from "@/lib/api";
import {
  OWNER_COST_SHARE_CATEGORIES,
  EXPENSE_CATEGORY_LABEL,
  BOOKING_TYPE_TAB_LABEL,
  BOOKING_TYPES_FOR_COST_SHARE,
  type CostShareRuleItem,
  type CostShareRuleOut,
  type OwnerCostShareCategory,
} from "@/lib/cost-share-constants";
import type { BookingType } from "@/lib/types";
import { tokens } from "@/lib/design-tokens";

const { Text } = Typography;

interface Props {
  roomId: string;
}

// 默认值：业主全担（与王总参考截图 100% 一致）
const DEFAULT_PERCENT = 1.0;

export default function CostShareEditor({ roomId }: Props) {
  const qc = useQueryClient();
  const [activeBookingType, setActiveBookingType] = useState<BookingType>("normal");
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkPercent, setBulkPercent] = useState<number | null>(1.0);
  const [bulkUnitPrice, setBulkUnitPrice] = useState<number | null>(null);

  const { data: rules, isLoading } = useQuery({
    queryKey: ["room-cost-share", roomId],
    queryFn: () => roomsApi.costShareRules.list(roomId).then((r) => r.data),
    enabled: !!roomId,
  });

  // 转 map: (booking_type, category) → rule
  const ruleMap = useMemo(() => {
    const map = new Map<string, CostShareRuleOut>();
    (rules || []).forEach((r) => {
      map.set(`${r.booking_type}::${r.expense_category}`, r);
    });
    return map;
  }, [rules]);

  const upsertMutation = useMutation({
    mutationFn: (items: CostShareRuleItem[]) =>
      roomsApi.costShareRules.upsert(roomId, items).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["room-cost-share", roomId] });
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "保存失败")),
  });

  const handleCellChange = (
    bookingType: BookingType,
    category: OwnerCostShareCategory,
    field: "share_percent" | "unit_price",
    value: number | null
  ) => {
    const existing = ruleMap.get(`${bookingType}::${category}`);
    const item: CostShareRuleItem = {
      booking_type: bookingType,
      expense_category: category,
      share_percent: existing?.share_percent ?? DEFAULT_PERCENT,
      unit_price: existing?.unit_price ?? null,
    };
    if (field === "share_percent") {
      if (value === null) return;
      if (Math.abs((existing?.share_percent ?? DEFAULT_PERCENT) - value) < 0.00005) return;
      item.share_percent = value;
    } else {
      if (value === existing?.unit_price) return;
      item.unit_price = value;
    }
    upsertMutation.mutate([item]);
  };

  const handleBulkApply = () => {
    if (bulkPercent === null) {
      message.warning("请输入占比");
      return;
    }
    const items: CostShareRuleItem[] = OWNER_COST_SHARE_CATEGORIES.map((cat) => ({
      booking_type: activeBookingType,
      expense_category: cat,
      share_percent: bulkPercent,
      unit_price: bulkUnitPrice,
    }));
    upsertMutation.mutate(items, {
      onSuccess: () => {
        message.success(
          `已批量更新 ${BOOKING_TYPE_TAB_LABEL[activeBookingType]} 的所有 14 个类目`
        );
        setBulkOpen(false);
      },
    });
  };

  const renderTabContent = (bookingType: BookingType) => (
    <div>
      <Space style={{ marginBottom: 12, justifyContent: "space-between", width: "100%" }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          业主承担占比 0~100%（1.0 = 业主全担）；单价为可选固定金额（与占比互斥）。失焦自动保存。
        </Text>
        <Button
          size="small"
          onClick={() => {
            setBulkPercent(1.0);
            setBulkUnitPrice(null);
            setBulkOpen(true);
          }}
        >
          批量更改
        </Button>
      </Space>
      <Table
        rowKey="category"
        size="small"
        loading={isLoading}
        pagination={false}
        dataSource={OWNER_COST_SHARE_CATEGORIES.map((cat) => {
          const rule = ruleMap.get(`${bookingType}::${cat}`);
          return {
            category: cat,
            label: EXPENSE_CATEGORY_LABEL[cat],
            share_percent: rule?.share_percent ?? DEFAULT_PERCENT,
            unit_price: rule?.unit_price ?? null,
          };
        })}
        columns={[
          { title: "费用类目", dataIndex: "label", width: 140 },
          {
            title: "占比 (%)",
            dataIndex: "share_percent",
            width: 140,
            render: (v, row) => (
              <InputNumber
                size="small"
                min={0}
                max={1}
                step={0.05}
                precision={4}
                style={{ width: 110 }}
                defaultValue={Number(v)}
                onBlur={(e) => {
                  const num = e.target.value === "" ? null : Number(e.target.value);
                  handleCellChange(bookingType, row.category, "share_percent", num);
                }}
                formatter={(value) =>
                  value !== undefined && value !== null
                    ? `${(Number(value) * 100).toFixed(0)}%`
                    : ""
                }
                parser={(value) => Number((value || "").replace("%", "")) / 100}
              />
            ),
          },
          {
            title: "单价 (¥，选填)",
            dataIndex: "unit_price",
            width: 140,
            render: (v, row) => (
              <InputNumber
                size="small"
                min={0}
                precision={2}
                style={{ width: 110 }}
                defaultValue={v !== null && v !== undefined ? Number(v) : undefined}
                placeholder="¥单价"
                onBlur={(e) => {
                  const num = e.target.value === "" ? null : Number(e.target.value);
                  handleCellChange(bookingType, row.category, "unit_price", num);
                }}
              />
            ),
          },
        ]}
      />
    </div>
  );

  return (
    <div style={{ padding: "8px 0" }}>
      <Tabs
        activeKey={activeBookingType}
        onChange={(k) => setActiveBookingType(k as BookingType)}
        items={BOOKING_TYPES_FOR_COST_SHARE.map((bt) => ({
          key: bt,
          label: BOOKING_TYPE_TAB_LABEL[bt],
          children: renderTabContent(bt),
        }))}
      />

      <Modal
        open={bulkOpen}
        title={`批量更改 — ${BOOKING_TYPE_TAB_LABEL[activeBookingType]}（14 个类目）`}
        onCancel={() => setBulkOpen(false)}
        onOk={handleBulkApply}
        okText="一键应用"
        cancelText="取消"
        confirmLoading={upsertMutation.isPending}
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Text type="secondary" style={{ fontSize: 12 }}>
            将此值应用到当前订单类型下的所有 14 个费用类目。
          </Text>
          <div>
            <Text strong style={{ fontSize: 13, display: "block", marginBottom: 6 }}>
              业主承担占比
            </Text>
            <InputNumber
              min={0}
              max={1}
              step={0.05}
              precision={4}
              style={{ width: 200 }}
              value={bulkPercent}
              onChange={(v) => setBulkPercent(v as number | null)}
              formatter={(value) =>
                value !== undefined && value !== null
                  ? `${(Number(value) * 100).toFixed(0)}%`
                  : ""
              }
              parser={(value) => Number((value || "").replace("%", "")) / 100}
            />
          </div>
          <div>
            <Text strong style={{ fontSize: 13, display: "block", marginBottom: 6 }}>
              单价 (¥, 选填)
            </Text>
            <InputNumber
              min={0}
              precision={2}
              style={{ width: 200 }}
              value={bulkUnitPrice}
              onChange={(v) => setBulkUnitPrice(v as number | null)}
              placeholder="留空表示不设单价"
            />
          </div>
        </Space>
      </Modal>
    </div>
  );
}
