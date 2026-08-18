"use client";

/**
 * 「业主与分成」合并页 · Tab 1: 按业主看
 *
 * 内容:业主 CRUD + 名下房间展开 + 批量关联房间(含 v1 老字段:
 * deduction_rules / ignored_categories)+ 业主密码重置。
 *
 * v1 老字段(deduction_rules / ignored_categories)在「关联房间」和
 * 「单房编辑」Drawer 里仍可改 — 后端 settlements 里有 v2→v1 fallback,
 * 已有数据需保留可维护。新房用户应优先在「按房间看」Tab 配 v2 费用占比。
 */
import React, { useState } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import {
  useQuery, useMutation, useQueryClient,
} from "@tanstack/react-query";
import {
  Card, Table, Button, Modal, Form, Input, message,
  Space, Popconfirm, Typography, Row, Col, Drawer, Slider,
  Checkbox, Divider, Segmented, Select, Tag,
} from "antd";
import {
  PlusOutlined, EditOutlined, DeleteOutlined,
  HomeOutlined, SafetyOutlined,
} from "@ant-design/icons";
import { ownersApi, roomsApi } from "@/lib/api";
import type { OwnerOut, OwnerRoomItem, RoomOut } from "@/lib/types";
import { tokens } from "@/lib/design-tokens";

const { Text } = Typography;

// 全量 label（v2#7 对齐后端 EXPENSE_CATEGORY_LABELS）。历史支出含旧类目，缺项会显英文原文。
const EXPENSE_CATEGORIES: Record<string, string> = {
  cleaning: "保洁",
  maintenance: "维修费",
  utilities: "水电煤（旧）",
  supplies: "采购费",
  platform_fee: "平台手续费",
  tax: "税费",
  other: "其他",
  public_utilities: "公摊水费",
  water: "水费",
  cold_water: "冷水（旧）",
  broadband: "宽带费",
  daily_supplies: "日耗",
  laundry: "洗涤",
  hot_water: "热水（旧）",
  gas: "燃气费",
  property_fee: "物业费",
  electricity: "电费",
  kitchen_cleaning: "厨房保洁",
  property_guidance_fee: "物业引导费",
  new_linen_prewash: "新布草过水费",
};

function isMobileDevice() {
  if (typeof window === "undefined") return false;
  return window.innerWidth < 1024;
}

export default function OwnersTab() {
  const qc = useQueryClient();
  const [ownerModalOpen, setOwnerModalOpen] = useState(false);
  const [editingOwner, setEditingOwner] = useState<OwnerOut | null>(null);
  const [assignOpen, setAssignOpen] = useState(false);
  const [assignOwnerId, setAssignOwnerId] = useState<string>("");
  const [selectedRoomIds, setSelectedRoomIds] = useState<string[]>([]);
  const [assignRatio, setAssignRatio] = useState<number>(60);
  const [assignRules, setAssignRules] = useState<string[]>([
    "cleaning",
    "maintenance",
    "platform_fee",
  ]);
  const [assignIgnored, setAssignIgnored] = useState<string[]>([]);

  type CategoryPolicy = "company" | "owner" | "ignored";
  const policyFor = (cat: string): CategoryPolicy => {
    if (assignIgnored.includes(cat)) return "ignored";
    if (assignRules.includes(cat)) return "owner";
    return "company";
  };
  const setPolicyFor = (cat: string, p: CategoryPolicy) => {
    setAssignRules((prev) => (p === "owner" ? Array.from(new Set([...prev, cat])) : prev.filter((c) => c !== cat)));
    setAssignIgnored((prev) => (p === "ignored" ? Array.from(new Set([...prev, cat])) : prev.filter((c) => c !== cat)));
  };
  const [roomEditOpen, setRoomEditOpen] = useState(false);
  const [editingRoom, setEditingRoom] = useState<OwnerRoomItem | null>(null);
  const [editRoomOwnerId, setEditRoomOwnerId] = useState<string>("");

  const [resetOpen, setResetOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState<OwnerOut | null>(null);
  const [resetForm] = Form.useForm();

  const [form] = Form.useForm();

  const { data: owners, isLoading } = useQuery({
    queryKey: ["owners"],
    queryFn: () => ownersApi.list().then((r) => r.data),
  });

  const { data: allRooms } = useQuery({
    queryKey: ["rooms"],
    queryFn: () => roomsApi.list().then((r) => r.data),
  });

  const saveOwnerMutation = useMutation({
    mutationFn: (vals: any) =>
      editingOwner
        ? ownersApi.update(editingOwner.owner_id, vals)
        : ownersApi.create(vals),
    onSuccess: () => {
      message.success(editingOwner ? "业主已更新" : "业主已创建");
      qc.invalidateQueries({ queryKey: ["owners"] });
      setOwnerModalOpen(false);
      form.resetFields();
    },
    onError: (e: any) =>
      message.error(extractErrorMessage(e, "保存失败")),
  });

  const deleteOwnerMutation = useMutation({
    mutationFn: (id: string) => ownersApi.delete(id),
    onSuccess: () => {
      message.success("业主已删除");
      qc.invalidateQueries({ queryKey: ["owners"] });
    },
    onError: (e: any) =>
      message.error(extractErrorMessage(e, "删除失败")),
  });

  const resetPasswordMutation = useMutation({
    mutationFn: (payload: { owner_id: string; new_password: string }) =>
      ownersApi.resetPassword(payload.owner_id, payload.new_password),
    onSuccess: () => {
      message.success("密码已重置");
      setResetOpen(false);
      resetForm.resetFields();
    },
    onError: (e: any) =>
      message.error(extractErrorMessage(e, "重置密码失败")),
  });

  const assignMutation = useMutation({
    mutationFn: (data: {
      room_ids: string[];
      owner_id?: string | null;
      owner_share_ratio?: number;
      owner_deduction_rules?: string[];
      owner_ignored_categories?: string[];
    }) => ownersApi.batchAssignRooms(data),
    onSuccess: () => {
      message.success("房间关联已更新");
      qc.invalidateQueries({ queryKey: ["owners"] });
      qc.invalidateQueries({ queryKey: ["rooms"] });
      setAssignOpen(false);
      setSelectedRoomIds([]);
      setRoomEditOpen(false);
      setEditingRoom(null);
    },
    onError: (e: any) =>
      message.error(extractErrorMessage(e, "更新失败")),
  });

  const openNewOwner = () => {
    setEditingOwner(null);
    form.resetFields();
    setOwnerModalOpen(true);
  };

  const openEditOwner = (owner: OwnerOut) => {
    setEditingOwner(owner);
    form.setFieldsValue({ ...owner, parent_owner_id: owner.parent_owner_id ?? undefined });
    setOwnerModalOpen(true);
  };

  const openAssign = (ownerId: string) => {
    setAssignOwnerId(ownerId);
    setSelectedRoomIds([]);
    setAssignRatio(60);
    setAssignRules(["cleaning", "maintenance", "platform_fee"]);
    setAssignIgnored([]);
    setAssignOpen(true);
  };

  const openRoomEdit = (ownerId: string, room: OwnerRoomItem) => {
    setEditingRoom(room);
    setEditRoomOwnerId(ownerId);
    setAssignRatio(Math.round(Number(room.owner_share_ratio) * 100));
    setAssignRules(room.owner_deduction_rules || []);
    setAssignIgnored(room.owner_ignored_categories || []);
    setRoomEditOpen(true);
  };

  // 未被任何业主关联的房间(可分配)
  const unassignedRooms: RoomOut[] = (allRooms || []).filter((r) => !r.owner_id);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Space style={{ justifyContent: "flex-end", width: "100%" }}>
        <Button icon={<PlusOutlined />} type="primary" onClick={openNewOwner}>
          新建业主
        </Button>
      </Space>

      <Table
        dataSource={owners ?? []}
        rowKey="owner_id"
        loading={isLoading}
        pagination={false}
        scroll={{ x: 760 }}
        expandable={{
          expandedRowRender: (owner) => (
            <div style={{ padding: "8px 16px" }}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: 10,
                }}
              >
                <Text strong>名下房间({owner.rooms.length} 套)</Text>
                <Button size="small" icon={<HomeOutlined />} onClick={() => openAssign(owner.owner_id)}>
                  关联更多房间
                </Button>
              </div>
              {owner.rooms.length === 0 ? (
                <Text type="secondary">暂无关联房间,点击右上角可批量添加</Text>
              ) : (
                <Table
                  size="small"
                  dataSource={owner.rooms}
                  rowKey="room_id"
                  pagination={false}
                  columns={[
                    { title: "房号", dataIndex: "room_id", width: 80 },
                    { title: "房名", dataIndex: "room_name", width: 160 },
                    {
                      title: "",
                      width: 140,
                      render: (_, r) => (
                        <Space>
                          <Button
                            size="small"
                            icon={<EditOutlined />}
                            onClick={() => openRoomEdit(owner.owner_id, r)}
                          >
                            修改
                          </Button>
                          <Popconfirm
                            title="解除该房间与业主的关联?"
                            onConfirm={() =>
                              assignMutation.mutate({
                                room_ids: [r.room_id],
                                owner_id: null,
                              })
                            }
                          >
                            <Button size="small" danger>
                              解除
                            </Button>
                          </Popconfirm>
                        </Space>
                      ),
                    },
                  ]}
                />
              )}
            </div>
          ),
        }}
        columns={[
          {
            title: "业主",
            dataIndex: "name",
            width: 150,
            fixed: "left" as const,
            render: (v, r) => (
              <Space>
                <span
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: "50%",
                    background: tokens.color.brand.primarySoft,
                    color: tokens.color.brand.primary,
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontWeight: 600,
                  }}
                >
                  {v.charAt(0)}
                </span>
                <div>
                  <div style={{ fontWeight: 500 }}>
                    {v}
                    {r.is_master && (
                      <Tag color="gold" style={{ marginLeft: 6 }}>
                        总账号 · 共管 {r.sub_owners?.length ?? 0} 层
                      </Tag>
                    )}
                    {!r.is_master && r.parent_owner_id && (
                      <Tag color="blue" style={{ marginLeft: 6 }}>
                        子账号
                      </Tag>
                    )}
                  </div>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {r.owner_id}
                  </Text>
                </div>
              </Space>
            ),
          },
          {
            title: "登录账号",
            dataIndex: "username",
            width: 140,
            render: (v) =>
              v ? (
                <Text code>{v}</Text>
              ) : (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  未设置
                </Text>
              ),
          },
          { title: "电话", dataIndex: "phone", width: 130 },
          { title: "银行账号", dataIndex: "bank_account", width: 180 },
          {
            title: "房间数",
            dataIndex: "room_count",
            width: 90,
            align: "right",
            render: (v) => <Text strong>{v}</Text>,
          },
          {
            title: "操作",
            width: 260,
            render: (_, r) => (
              <Space>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEditOwner(r)}>
                  编辑
                </Button>
                <Button
                  size="small"
                  icon={<SafetyOutlined />}
                  onClick={() => {
                    setResetTarget(r);
                    setResetOpen(true);
                  }}
                >
                  重置密码
                </Button>
                <Popconfirm
                  title={`删除业主 "${r.name}"?`}
                  description="仅在名下无房间时可删除"
                  onConfirm={() => deleteOwnerMutation.mutate(r.owner_id)}
                >
                  <Button size="small" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      {/* 业主 CRUD Modal */}
      <Modal
        open={ownerModalOpen}
        onCancel={() => setOwnerModalOpen(false)}
        title={editingOwner ? `编辑业主 ${editingOwner.name}` : "新建业主"}
        onOk={() => form.submit()}
        confirmLoading={saveOwnerMutation.isPending}
        okText="保存"
        cancelText="取消"
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(vals) =>
            saveOwnerMutation.mutate(
              editingOwner
                ? { ...vals, parent_owner_id: vals.parent_owner_id ?? "" }
                : vals
            )
          }
        >
          <Form.Item name="name" label="姓名" rules={[{ required: true }]}>
            <Input placeholder="如:王某某" />
          </Form.Item>
          <Form.Item
            name="username"
            label="登录账号"
            tooltip="业主登录业主端时使用的账号，4-50 位字母、数字、下划线、点"
            rules={[
              {
                pattern: /^[A-Za-z0-9_.]{4,50}$/,
                message: "账号需 4-50 位，仅允许字母、数字、下划线、点",
              },
            ]}
            normalize={(v) => (typeof v === "string" ? v.trim().toLowerCase() : v)}
          >
            <Input placeholder="如:wangmou01（留空则该业主无法用账号密码登录）" autoComplete="off" />
          </Form.Item>
          <Row gutter={12}>
            <Col xs={24} sm={12}>
              <Form.Item name="phone" label="电话">
                <Input placeholder="手机号" />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item name="id_card" label="身份证号">
                <Input placeholder="(加密存储)" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="bank_name" label="开户行">
            <Input placeholder="如:工商银行" />
          </Form.Item>
          <Form.Item name="bank_account" label="银行账号">
            <Input placeholder="用于月度结算打款" />
          </Form.Item>
          <Form.Item name="notes" label="备注">
            <Input.TextArea rows={2} maxLength={500} />
          </Form.Item>
          {editingOwner && (
            <Form.Item
              name="parent_owner_id"
              label="上级总账号（可选）"
              tooltip="绑定后该业主成为子账号，其数据可在总账号下按层查看"
            >
              <Select
                allowClear
                placeholder="无（独立业主，默认）"
                options={(owners ?? [])
                  .filter((o) => o.owner_id !== editingOwner.owner_id)
                  .map((o) => ({ label: o.name, value: o.owner_id }))}
              />
            </Form.Item>
          )}
        </Form>
      </Modal>

      {/* 批量关联房间 Drawer */}
      <Drawer
        open={assignOpen}
        onClose={() => setAssignOpen(false)}
        title={`关联房间到业主 ${assignOwnerId}`}
        width={isMobileDevice() ? "100%" : 560}
        extra={
          <Button
            type="primary"
            disabled={selectedRoomIds.length === 0}
            onClick={() =>
              assignMutation.mutate({
                room_ids: selectedRoomIds,
                owner_id: assignOwnerId,
                owner_share_ratio: assignRatio / 100,
                owner_deduction_rules: assignRules,
                owner_ignored_categories: assignIgnored,
              })
            }
            loading={assignMutation.isPending}
          >
            确认关联({selectedRoomIds.length} 套)
          </Button>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card size="small" title="分成规则">
            <div style={{ marginBottom: 12 }}>
              <Text strong>分成比例:</Text>
              <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                业主拿 {assignRatio}%,公司拿 {100 - assignRatio}%
              </Text>
            </div>
            <Slider
              value={assignRatio}
              onChange={setAssignRatio}
              min={0}
              max={100}
              step={5}
              marks={{ 0: "0%", 50: "50%", 70: "70%", 100: "100%" }}
            />
            <Divider style={{ margin: "12px 0" }} />
            <div>
              <Text strong>各类支出分账策略(老规则):</Text>
              <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
                {Object.entries(EXPENSE_CATEGORIES).map(([k, label]) => (
                  <div key={k} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <Text style={{ minWidth: 80 }}>{label}</Text>
                    <Segmented
                      size="small"
                      value={policyFor(k)}
                      onChange={(v) => setPolicyFor(k, v as any)}
                      options={[
                        { label: "公司承担", value: "company" },
                        { label: "业主承担", value: "owner" },
                        { label: "不计入", value: "ignored" },
                      ]}
                    />
                  </div>
                ))}
              </div>
              <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 10 }}>
                这是 v1 老规则,仅在该房没有「按房间看」Tab 配置的费用占比规则时生效。
                新房建议直接到「按房间看」Tab 用 14 类目 × 3 订单类型矩阵配置。
              </Text>
            </div>
          </Card>

          <Card size="small" title={`选择房间(未关联业主的:${unassignedRooms.length} 套)`}>
            {unassignedRooms.length === 0 ? (
              <Text type="secondary">没有可用的未关联房间</Text>
            ) : (
              <Checkbox.Group
                value={selectedRoomIds}
                onChange={(v) => setSelectedRoomIds(v as string[])}
                style={{ display: "flex", flexDirection: "column", gap: 8 }}
              >
                {unassignedRooms.map((r) => (
                  <Checkbox key={r.room_id} value={r.room_id}>
                    <Text strong>{r.room_id}</Text>{" "}
                    <Text type="secondary">· {r.room_name}</Text>
                  </Checkbox>
                ))}
              </Checkbox.Group>
            )}
          </Card>
        </div>
      </Drawer>

      {/* 重置密码 Modal */}
      <Modal
        title={resetTarget ? `重置密码:${resetTarget.name}(${resetTarget.owner_id})` : "重置密码"}
        open={resetOpen}
        onCancel={() => {
          setResetOpen(false);
          resetForm.resetFields();
        }}
        onOk={() => resetForm.submit()}
        confirmLoading={resetPasswordMutation.isPending}
        okText="确定"
        cancelText="取消"
        destroyOnHidden
      >
        {resetTarget && !resetTarget.username && (
          <div
            style={{
              background: "#FFF7E6",
              border: "1px solid #FFE7BA",
              color: "#AD6800",
              padding: "8px 12px",
              borderRadius: 6,
              marginBottom: 12,
              fontSize: 12,
            }}
          >
            该业主尚未设置登录账号，请先到「编辑」中填写账号，否则即使设了密码也无法登录业主端。
          </div>
        )}
        {resetTarget?.username && (
          <div style={{ marginBottom: 12, fontSize: 13, color: "#666" }}>
            登录账号：<Text code>{resetTarget.username}</Text>
          </div>
        )}
        <Form
          form={resetForm}
          layout="vertical"
          onFinish={(values) => {
            if (!resetTarget) return;
            resetPasswordMutation.mutate({
              owner_id: resetTarget.owner_id,
              new_password: values.new_password,
            });
          }}
        >
          <Form.Item
            label="新密码"
            name="new_password"
            rules={[
              { required: true, message: "请输入新密码" },
              { min: 8, message: "密码至少 8 位" },
              {
                validator: (_, v: string) => {
                  if (!v) return Promise.resolve();
                  if (/^\d+$/.test(v) || /^[A-Za-z]+$/.test(v)) {
                    return Promise.reject(new Error("密码必须同时包含字母和数字"));
                  }
                  return Promise.resolve();
                },
              },
            ]}
          >
            <Input.Password placeholder="例如:Abc12345" />
          </Form.Item>
          <Text type="secondary" style={{ fontSize: 12 }}>
            提示:系统不会保存明文密码,请将账号与新密码安全地告知业主,建议业主登录后立即在「我的」-「修改密码」中改成自己的常用密码。
          </Text>
        </Form>
      </Modal>

      {/* 单房编辑 Drawer */}
      <Drawer
        open={roomEditOpen}
        onClose={() => setRoomEditOpen(false)}
        title={`调整房间 ${editingRoom?.room_id} · ${editingRoom?.room_name} 的分成规则`}
        width={isMobileDevice() ? "100%" : 520}
        extra={
          <Button
            type="primary"
            onClick={() =>
              editingRoom &&
              assignMutation.mutate({
                room_ids: [editingRoom.room_id],
                owner_id: editRoomOwnerId,
                owner_share_ratio: assignRatio / 100,
                owner_deduction_rules: assignRules,
                owner_ignored_categories: assignIgnored,
              })
            }
            loading={assignMutation.isPending}
          >
            保存
          </Button>
        }
      >
        {editingRoom && (
          <div>
            <Card size="small" title="普通订单分成比例" style={{ marginBottom: 12 }}>
              <Text type="secondary">业主 {assignRatio}% / 公司 {100 - assignRatio}%</Text>
              <Slider
                value={assignRatio}
                onChange={setAssignRatio}
                min={0}
                max={100}
                step={5}
                marks={{ 0: "0%", 50: "50%", 70: "70%", 100: "100%" }}
                style={{ marginTop: 12 }}
              />
              <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
                试住单 / 自住单的分成比例请到「按房间看」Tab 设置。
              </Text>
            </Card>
            <Card size="small" title="各类支出分账策略(老规则)">
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {Object.entries(EXPENSE_CATEGORIES).map(([k, label]) => (
                  <div key={k} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <Text style={{ minWidth: 80 }}>{label}</Text>
                    <Segmented
                      size="small"
                      value={policyFor(k)}
                      onChange={(v) => setPolicyFor(k, v as any)}
                      options={[
                        { label: "公司承担", value: "company" },
                        { label: "业主承担", value: "owner" },
                        { label: "不计入", value: "ignored" },
                      ]}
                    />
                  </div>
                ))}
              </div>
              <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 10 }}>
                这是 v1 老规则。新建房间建议直接到「按房间看」Tab 用 14 类目 × 3 订单类型矩阵配置。
              </Text>
            </Card>
          </div>
        )}
      </Drawer>
    </div>
  );
}
