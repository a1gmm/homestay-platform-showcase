"use client";

import { Col, Tooltip, Popconfirm, Button, Dropdown, Tag } from "antd";
import type { MenuProps } from "antd";
import {
  EditOutlined,
  LinkOutlined,
  MoreOutlined,
  DeleteOutlined,
  FormOutlined,
  HomeOutlined,
} from "@ant-design/icons";
import { ROOM_STATUS } from "./constants";
import { buildLockMenuItems, type LockBlockType } from "./room-lock-actions";
import type { RoomItem, PricingDay } from "./types";
import { useIsMobile } from "@/lib/responsive";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { tokens } from "@/lib/design-tokens";

interface RoomCardProps {
  room: RoomItem;
  expanded: boolean;
  pricingDays?: PricingDay[];
  onToggleExpand: () => void;
  onOpenStatusModal: () => void;
  onDelete: () => void;
  onOpenEditInfo: () => void;
  onLock: (blockType: LockBlockType) => void;
  onOpenPricingDetail?: (date: string, sourceUrl: string | null) => void;
}

const STATUS_ACCENT: Record<string, string> = {
  available: "#7B8578",     // sage
  occupied: "#2B2721",      // ink
  reserved: "#6B665B",      // stone
  pending_clean: "#C19B6E", // 暖棕
  cleaning: "#D4A017",      // 琥珀
  maintenance: "#8A6E5A",   // clay
  locked: "#A89680",        // driftwood
};

export function RoomCard({
  room,
  expanded,
  pricingDays,
  onToggleExpand,
  onOpenStatusModal,
  onDelete,
  onOpenEditInfo,
  onLock,
  onOpenPricingDetail,
}: RoomCardProps) {
  const isMobile = useIsMobile();
  const todayPricing = pricingDays?.[0];
  const shown = room.effective_status ?? room.room_status;
  const accent = STATUS_ACCENT[shown] ?? tokens.color.gray[500];
  const label = ROOM_STATUS[shown]?.label ?? shown;

  // 锁房三类置顶（一步直达锁房弹窗），原有动作下移 —— 管家反馈「点房号直接锁房」
  const menuItems: MenuProps["items"] = [
    ...buildLockMenuItems(onLock),
    { type: "divider" },
    { key: "editStatus", label: "修改状态", icon: <EditOutlined />, onClick: onOpenStatusModal },
    { key: "editInfo", label: "编辑信息", icon: <FormOutlined />, onClick: onOpenEditInfo },
    { type: "divider" },
    { key: "delete", label: "下线房间", icon: <DeleteOutlined />, danger: true, onClick: onDelete },
  ];

  return (
    <Col xs={24} sm={12} md={8} lg={6} xl={6}>
      <div
        className="card-hoverable"
        onClick={onToggleExpand}
        style={{
          background: tokens.color.bg.container,
          border: `1px solid ${tokens.color.bg.border}`,
          borderRadius: tokens.radius.lg,
          overflow: "hidden",
          cursor: "pointer",
          boxShadow: tokens.shadow.sm,
          display: "flex",
          flexDirection: "column",
          height: "100%",
        }}
      >
        {/* Top cover — colored gradient strip with room id */}
        <div
          style={{
            height: 72,
            background: `linear-gradient(135deg, ${accent}22, ${accent}44)`,
            position: "relative",
            padding: "12px 14px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div
            style={{
              width: 36,
              height: 36,
              borderRadius: tokens.radius.md,
              background: "rgba(255,255,255,.85)",
              color: accent,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              boxShadow: tokens.shadow.sm,
            }}
          >
            <HomeOutlined style={{ fontSize: 16 }} />
          </div>
          <Dropdown menu={{ items: menuItems }} trigger={["click"]} placement="bottomRight">
            <Button
              type="text"
              icon={<MoreOutlined />}
              size="small"
              onClick={(e) => e.stopPropagation()}
              style={{ color: tokens.color.text.secondary }}
            />
          </Dropdown>
        </div>

        <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 10, flex: 1 }}>
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8 }}>
            <div style={{ minWidth: 0 }}>
              {/* 点房号/房名 → 同一动作面板（锁房置顶）。stopPropagation 避免触发卡片展开房价 */}
              <Dropdown menu={{ items: menuItems }} trigger={["click"]} placement="bottomLeft">
                <div
                  onClick={(e) => e.stopPropagation()}
                  style={{ cursor: "pointer", display: "inline-block", minWidth: 0, maxWidth: "100%" }}
                >
                  <div style={{ fontSize: 16, fontWeight: 600, color: tokens.color.text.primary, lineHeight: 1.2 }}>
                    {room.room_id}
                  </div>
                  <div
                    style={{
                      fontSize: 12,
                      color: tokens.color.text.tertiary,
                      marginTop: 2,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                    title={room.room_name}
                  >
                    {room.room_name}
                  </div>
                </div>
              </Dropdown>
              {/* 房型分组标签：填了显示房型；未填显示提示 */}
              <div style={{ marginTop: 6 }}>
                {room.room_type ? (
                  <Tooltip title="房间分组（房型）">
                    <Tag
                      bordered={false}
                      style={{
                        background: tokens.color.brand.primarySoft,
                        color: tokens.color.brand.primary,
                        fontSize: 11,
                        margin: 0,
                      }}
                    >
                      {room.room_type}
                    </Tag>
                  </Tooltip>
                ) : (
                  <Tooltip title="还未归类到房型，建议点「编辑信息」补充">
                    <Tag
                      bordered={false}
                      style={{ background: "#FFF7E6", color: "#D46B08", fontSize: 11, margin: 0 }}
                    >
                      未设置房型
                    </Tag>
                  </Tooltip>
                )}
              </div>
            </div>
            <StatusBadge status={shown} label={label} size="sm" />
          </div>

          {(room.city || room.community_name) && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
                color: tokens.color.text.secondary,
              }}
            >
              <span
                style={{
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  minWidth: 0,
                }}
              >
                {room.city ?? room.province ?? ""}
                {room.community_name ? ` · ${room.community_name}` : ""}
              </span>
            </div>
          )}

          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              justifyContent: "space-between",
              borderTop: `1px solid ${tokens.color.bg.borderSubtle}`,
              paddingTop: 10,
            }}
          >
            <div>
              <div style={{ fontSize: 11, color: tokens.color.text.tertiary }}>基础房价</div>
              <div className="tabular" style={{ fontSize: 16, fontWeight: 600 }}>
                ¥{room.base_price ?? "—"}
                <span style={{ fontSize: 11, color: tokens.color.text.tertiary, marginLeft: 2 }}>
                  /晚
                </span>
              </div>
            </div>
            {todayPricing?.price != null && todayPricing.price > 0 && (
              <div style={{ textAlign: "right" }}>
                <div style={{ fontSize: 11, color: tokens.color.text.tertiary }}>今日推荐</div>
                <div
                  className="tabular"
                  style={{
                    fontSize: 13,
                    fontWeight: 600,
                    color: tokens.color.brand.primary,
                  }}
                >
                  ¥{todayPricing.price.toFixed(0)}
                </div>
              </div>
            )}
          </div>

          {expanded && pricingDays && pricingDays.length > 0 && (
            <div
              style={{
                borderTop: `1px dashed ${tokens.color.bg.border}`,
                paddingTop: 10,
              }}
            >
              <div
                style={{
                  fontSize: 11,
                  color: tokens.color.text.tertiary,
                  marginBottom: 6,
                }}
              >
                未来 7 天价格
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                {pricingDays
                  .slice(0, 7)
                  .filter((d) => d.price !== null)
                  .map((day) => {
                    const d = new Date(day.date);
                    const label = `${d.getMonth() + 1}/${d.getDate()}`;
                    const hasSource = !!day.source_url;
                    return (
                      <Tooltip
                        key={day.date}
                        title={hasSource ? "查看竞品参考" : "手动定价"}
                      >
                        <div
                          style={{
                            padding: isMobile ? "5px 7px" : "4px 6px",
                            borderRadius: 6,
                            background: hasSource
                              ? tokens.color.brand.primarySoft
                              : tokens.color.bg.subtle,
                            color: hasSource
                              ? tokens.color.brand.primary
                              : tokens.color.text.primary,
                            cursor: hasSource ? "pointer" : "default",
                            transition: "transform 120ms ease",
                            fontSize: 12,
                            fontWeight: 500,
                            display: "flex",
                            flexDirection: "column",
                            alignItems: "center",
                            minWidth: 44,
                            lineHeight: 1.2,
                          }}
                          onClick={(e) => {
                            e.stopPropagation();
                            if (hasSource && onOpenPricingDetail) {
                              onOpenPricingDetail(day.date, day.source_url);
                            }
                          }}
                        >
                          <span style={{ fontSize: 10, color: tokens.color.text.tertiary }}>
                            {label}
                          </span>
                          <span>
                            ¥{day.price!.toFixed(0)}
                            {hasSource && (
                              <LinkOutlined style={{ fontSize: 9, marginLeft: 2 }} />
                            )}
                          </span>
                        </div>
                      </Tooltip>
                    );
                  })}
              </div>
            </div>
          )}
        </div>
      </div>
    </Col>
  );
}
