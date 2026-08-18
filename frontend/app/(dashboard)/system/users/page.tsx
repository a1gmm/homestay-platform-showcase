"use client";

import { useState, useEffect } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Card,
  Table,
  Button,
  Typography,
  Tag,
  Modal,
  Form,
  Input,
  Select,
  Space,
  message,
} from "antd";
import { PlusOutlined, StopOutlined, SafetyOutlined } from "@ant-design/icons";
import { usersApi } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";
import { useRouter } from "next/navigation";
import { PageHeader } from "@/components/ui/PageHeader";
import { useIsMobile } from "@/lib/responsive";
import { tokens } from "@/lib/design-tokens";

const { Title, Text } = Typography;

const ROLE_OPTIONS = [
  { value: "admin", label: "管理员" },
  { value: "operator", label: "运营" },
  { value: "finance", label: "财务" },
  { value: "cleaner", label: "保洁" },
  { value: "owner", label: "业主" },
];

// 角色标签字典(含 keeper 管家，修复移动端显示英文 role)
const ROLE_TAG: Record<string, { color: string; label: string }> = {
  admin: { color: "red", label: "管理员" },
  operator: { color: "blue", label: "运营" },
  finance: { color: "gold", label: "财务" },
  cleaner: { color: "green", label: "保洁" },
  keeper: { color: "cyan", label: "管家" },
  owner: { color: "purple", label: "业主" },
};

export default function UsersPage() {
  const qc = useQueryClient();
  const { user } = useAuthStore();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState<any | null>(null);
  const [form] = Form.useForm();
  const [resetForm] = Form.useForm();
  const isMobile = useIsMobile();
  const isAdmin = !!user && user.role === "admin";

  useEffect(() => {
    if (user && user.role !== "admin") {
      router.replace("/dashboard");
    }
  }, [user, router]);

  const { data, isLoading } = useQuery({
    queryKey: ["users", "admin-list"],
    enabled: isAdmin,
    queryFn: () => usersApi.list().then((r) => r.data),
  });

  const createMutation = useMutation({
    mutationFn: (values: any) => usersApi.create(values),
    onSuccess: () => {
      message.success("账号创建成功");
      setOpen(false);
      form.resetFields();
      // 用父前缀 ["users"] 失效：前缀匹配同时命中本页的 ["users","admin-list"]
      // 和任务页保洁姓名 lookup 的 ["users"]，保持改名前的跨页失效行为。
      qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (err: any) => {
      const msg =
        extractErrorMessage(err, "创建账号失败，请稍后重试");
      message.error(msg);
    },
  });

  const users = Array.isArray(data) ? data : [];

  const statusMutation = useMutation({
    mutationFn: (payload: { user_id: string; is_active: boolean }) =>
      usersApi.updateStatus(payload.user_id, payload.is_active),
    onSuccess: () => {
      message.success("账号状态已更新");
      // 用父前缀 ["users"] 失效：前缀匹配同时命中本页的 ["users","admin-list"]
      // 和任务页保洁姓名 lookup 的 ["users"]，保持改名前的跨页失效行为。
      qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (err: any) => {
      const msg =
        extractErrorMessage(err, "更新状态失败，请稍后重试");
      message.error(msg);
    },
  });

  const resetMutation = useMutation({
    mutationFn: (payload: { user_id: string; new_password: string }) =>
      usersApi.resetPassword(payload.user_id, payload.new_password),
    onSuccess: () => {
      message.success("密码已重置");
      setResetOpen(false);
      resetForm.resetFields();
    },
    onError: (err: any) => {
      const msg =
        extractErrorMessage(err, "重置密码失败，请稍后重试");
      message.error(msg);
    },
  });

  if (!isAdmin) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <PageHeader
        title="账号管理"
        subtitle="用于管理系统内部账号（员工、保洁、财务等），仅管理员可访问。"
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            新建账号
          </Button>
        }
      />

      {isMobile ? (
        // 移动端：账号卡片列表（替代横向溢出的 6 列表格）
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {users?.map((record: any) => {
            const isSelf = record.user_id === user?.user_id;
            const disableToggle = record.role === "admin" && !isSelf;
            const roleTag = ROLE_TAG[record.role] || { color: "default", label: record.role };
            return (
              <Card
                key={record.user_id}
                bordered={false}
                styles={{ body: { padding: 14 } }}
                style={{ borderRadius: 12, boxShadow: "0 2px 8px rgba(0,0,0,0.06)" }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 16, fontWeight: 600, minWidth: 0 }}>
                    {record.display_name || record.username}
                  </span>
                  <Tag color={roleTag.color} style={{ marginInlineEnd: 0 }}>{roleTag.label}</Tag>
                  {record.is_active ? (
                    <Tag color="success" style={{ marginInlineEnd: 0 }}>启用</Tag>
                  ) : (
                    <Tag color="default" style={{ marginInlineEnd: 0 }}>已禁用</Tag>
                  )}
                </div>
                <div style={{ marginTop: 6, fontSize: 13, color: tokens.color.text.secondary }}>
                  用户名 {record.username}
                  {record.phone ? ` · ${record.phone}` : ""}
                </div>
                <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                  <Button
                    size="small"
                    icon={<SafetyOutlined />}
                    onClick={() => {
                      setResetTarget(record);
                      setResetOpen(true);
                    }}
                  >
                    重置密码
                  </Button>
                  <Button
                    size="small"
                    icon={<StopOutlined />}
                    danger={record.is_active}
                    disabled={disableToggle}
                    onClick={() =>
                      statusMutation.mutate({ user_id: record.user_id, is_active: !record.is_active })
                    }
                  >
                    {record.is_active ? "禁用" : "启用"}
                  </Button>
                </div>
              </Card>
            );
          })}
        </div>
      ) : (
      <Card
        bordered={false}
        style={{ borderRadius: 12, boxShadow: "0 2px 8px rgba(0,0,0,0.06)" }}
      >
        <Table
          rowKey={(row: any) => row.user_id}
          loading={isLoading}
          dataSource={users}
          pagination={false}
          scroll={{ x: 720 }}
          columns={[
            {
              title: "用户名",
              dataIndex: "username",
            },
            {
              title: "显示名称",
              dataIndex: "display_name",
            },
            {
              title: "角色",
              dataIndex: "role",
              render: (role: string) => {
                const item = ROLE_TAG[role] || { color: "default", label: role };
                return <Tag color={item.color}>{item.label}</Tag>;
              },
            },
            {
              title: "手机号",
              dataIndex: "phone",
              render: (v: string | null) => v || "-",
            },
            {
              title: "状态",
              dataIndex: "is_active",
              render: (active: boolean) =>
                active ? (
                  <Tag color="success">启用</Tag>
                ) : (
                  <Tag color="default">已禁用</Tag>
                ),
            },
            {
              title: "操作",
              render: (_: any, record: any) => {
                const isSelf = record.user_id === user?.user_id;
                const isOtherAdmin = record.role === "admin" && !isSelf;
                const disableToggle = isOtherAdmin;
                return (
                  <Space size={8}>
                    <Button
                      type="link"
                      icon={<SafetyOutlined />}
                      onClick={() => {
                        setResetTarget(record);
                        setResetOpen(true);
                      }}
                    >
                      重置密码
                    </Button>
                    <Button
                      type="link"
                      icon={<StopOutlined />}
                      danger={record.is_active}
                      disabled={disableToggle}
                      onClick={() => {
                        statusMutation.mutate({
                          user_id: record.user_id,
                          is_active: !record.is_active,
                        });
                      }}
                    >
                      {record.is_active ? "禁用" : "启用"}
                    </Button>
                  </Space>
                );
              },
            },
          ]}
        />
      </Card>
      )}

      <Modal
        title="新建账号"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={createMutation.isPending}
        okText="创建"
        cancelText="取消"
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) => {
            createMutation.mutate(values);
          }}
          initialValues={{ role: "operator" }}
        >
          <Form.Item
            label="用户名"
            name="username"
            rules={[{ required: true, message: "请输入用户名" }]}
          >
            <Input placeholder="例如：frontdesk01" />
          </Form.Item>
          <Form.Item
            label="显示名称"
            name="display_name"
            rules={[{ required: true, message: "请输入显示名称" }]}
          >
            <Input placeholder="例如：前台-小王" />
          </Form.Item>
          <Form.Item
            label="手机号"
            name="phone"
            rules={[
              {
                pattern: /^1\d{10}$/,
                message: "请输入11位手机号码（可选）",
              },
            ]}
          >
            <Input placeholder="可选，例如：138xxxx0000" />
          </Form.Item>
          <Form.Item
            label="角色"
            name="role"
            rules={[{ required: true, message: "请选择角色" }]}
          >
            <Select options={ROLE_OPTIONS} />
          </Form.Item>
          <Form.Item
            label="初始密码"
            name="password"
            rules={[{ required: true, message: "请输入初始密码" }]}
          >
            <Input.Password placeholder="例如：Abc12345" />
          </Form.Item>

          <Form.Item>
            <Space direction="vertical" size={2}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                提示：系统不会保存明文密码，只保存加密结果。
              </Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                创建完成后请将初始密码告知员工，登录后可自行修改。
              </Text>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={resetTarget ? `重置密码：${resetTarget.username}` : "重置密码"}
        open={resetOpen}
        onCancel={() => {
          setResetOpen(false);
          resetForm.resetFields();
        }}
        onOk={() => resetForm.submit()}
        confirmLoading={resetMutation.isPending}
        okText="确定"
        cancelText="取消"
      >
        <Form
          form={resetForm}
          layout="vertical"
          onFinish={(values) => {
            if (!resetTarget) return;
            resetMutation.mutate({
              user_id: resetTarget.user_id,
              new_password: values.new_password,
            });
          }}
        >
          <Form.Item
            label="新密码"
            name="new_password"
            rules={[{ required: true, message: "请输入新密码" }]}
          >
            <Input.Password placeholder="例如：Abc12345" />
          </Form.Item>
          <Form.Item>
            <Text type="secondary" style={{ fontSize: 12 }}>
              提示：系统不会保存明文密码，只保存加密结果，请将新密码安全地告知员工。
            </Text>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

