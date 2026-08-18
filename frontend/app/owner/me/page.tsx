"use client";

import { useEffect, useState } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useRouter } from "next/navigation";
import { Button, Form, Input, Modal, Typography, message } from "antd";
import {
  HomeOutlined,
  PhoneOutlined,
  LogoutOutlined,
  CustomerServiceOutlined,
  LockOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { useOwnerStore } from "@/lib/owner-store";
import { useIsDesktop } from "@/lib/responsive";
import { ownerAuthApi } from "@/lib/owner-api";

const { Text } = Typography;

export default function OwnerMePage() {
  const router = useRouter();
  const owner = useOwnerStore((s) => s.owner);
  const isLoggedIn = useOwnerStore((s) => s.isLoggedIn());
  const clearAuth = useOwnerStore((s) => s.clearAuth);
  const isDesktop = useIsDesktop();

  const [pwOpen, setPwOpen] = useState(false);
  const [pwLoading, setPwLoading] = useState(false);
  const [pwForm] = Form.useForm();

  useEffect(() => {
    if (!isLoggedIn) {
      router.replace(`/owner/login?next=${encodeURIComponent("/owner/me")}`);
    }
  }, [isLoggedIn, router]);

  if (!isLoggedIn || !owner) return null;

  const logout = () => {
    clearAuth();
    message.success("已退出登录");
    router.replace("/owner/login");
  };

  const handleChangePassword = async (values: {
    current_password: string;
    new_password: string;
    confirm_password: string;
  }) => {
    setPwLoading(true);
    try {
      await ownerAuthApi.changePassword(values.current_password, values.new_password);
      message.success("密码已修改，请用新密码重新登录");
      setPwOpen(false);
      pwForm.resetFields();
      clearAuth();
      router.replace("/owner/login");
    } catch (e: any) {
      message.error(extractErrorMessage(e, "修改失败，请稍后重试"));
    } finally {
      setPwLoading(false);
    }
  };

  return (
    <div style={isDesktop ? { maxWidth: 680, margin: "0 auto", padding: "26px 26px 40px" } : undefined}>
      <div
        style={{
          padding: "32px 20px 28px",
          background: "#2B2721",
          color: "#FBF8F1",
          borderRadius: isDesktop ? 16 : 0,
        }}
      >
        <div
          className="serif"
          style={{
            width: 60,
            height: 60,
            borderRadius: "50%",
            background: "rgba(251, 248, 241, 0.1)",
            border: "0.5px solid rgba(251, 248, 241, 0.2)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 26,
            fontWeight: 400,
            marginBottom: 14,
          }}
        >
          {(owner.name || "业主").slice(0, 1)}
        </div>
        <div className="serif" style={{ fontSize: 22, fontWeight: 400, letterSpacing: 0 }}>
          {owner.name}
        </div>
        <div style={{ fontSize: 12, color: "#A89680", marginTop: 6 }}>
          业主编号 {owner.owner_id}
        </div>
      </div>

      <div style={{ padding: isDesktop ? "16px 0 0" : 16 }}>
        <div style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 12, overflow: "hidden" }}>
          <Row icon={<UserOutlined />} label="登录账号" value={owner.username || "未设置"} />
          <Row icon={<PhoneOutlined />} label="手机号" value={owner.phone || "未绑定"} />
          <Row
            icon={<HomeOutlined />}
            label="名下房源"
            value={
              owner.is_master
                ? `共管 ${owner.sub_owners?.length ?? 0} 层 · ${owner.room_count} 套房`
                : `${owner.room_count} 套`
            }
          />
          <Row
            icon={<CustomerServiceOutlined />}
            label="客服"
            value="请联系管家"
            last={!owner.is_master}
          />
        </div>

        {owner.is_master && owner.sub_owners && owner.sub_owners.length > 0 && (
          <div
            style={{
              background: "#F5F1EA",
              border: "0.5px solid #E5DDCB",
              borderRadius: 12,
              overflow: "hidden",
              marginTop: 12,
            }}
          >
            <div
              style={{
                padding: "10px 16px",
                fontSize: 12,
                color: "#A89680",
                borderBottom: "0.5px solid #E5DDCB",
              }}
            >
              共管楼层
            </div>
            {owner.sub_owners.map((s, i) => (
              <Row
                key={s.owner_id}
                icon={<HomeOutlined />}
                label={s.name}
                value={`${s.room_count} 套`}
                last={i === owner.sub_owners!.length - 1}
              />
            ))}
          </div>
        )}

        {owner.is_master && (
          <div style={{ fontSize: 11, color: "#A89680", marginTop: 10, padding: "0 4px" }}>
            总账号为只读汇总视图，房源图片等管理操作请由各层业主账号完成。
          </div>
        )}

        <Button
          block
          size="large"
          icon={<LockOutlined />}
          onClick={() => setPwOpen(true)}
          style={{ marginTop: 24, minHeight: 44 }}
        >
          修改密码
        </Button>

        <Button
          block
          size="large"
          icon={<LogoutOutlined />}
          onClick={logout}
          style={{ marginTop: 12, minHeight: 44 }}
          danger
        >
          退出登录
        </Button>
      </div>

      <Modal
        open={pwOpen}
        title="修改密码"
        onCancel={() => {
          setPwOpen(false);
          pwForm.resetFields();
        }}
        onOk={() => pwForm.submit()}
        confirmLoading={pwLoading}
        okText="确认修改"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={pwForm} layout="vertical" onFinish={handleChangePassword}>
          <Form.Item
            label="当前密码"
            name="current_password"
            rules={[{ required: true, message: "请输入当前密码" }]}
          >
            <Input.Password placeholder="输入当前登录密码" style={{ fontSize: 16 }} />
          </Form.Item>
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
            <Input.Password placeholder="至少 8 位，需含字母和数字" style={{ fontSize: 16 }} />
          </Form.Item>
          <Form.Item
            label="确认新密码"
            name="confirm_password"
            dependencies={["new_password"]}
            rules={[
              { required: true, message: "请再次输入新密码" },
              ({ getFieldValue }) => ({
                validator(_, v) {
                  if (!v || v === getFieldValue("new_password")) return Promise.resolve();
                  return Promise.reject(new Error("两次输入的密码不一致"));
                },
              }),
            ]}
          >
            <Input.Password placeholder="再次输入新密码" style={{ fontSize: 16 }} />
          </Form.Item>
          <Text type="secondary" style={{ fontSize: 12 }}>
            修改成功后将自动退出登录，请用新密码重新登录。
          </Text>
        </Form>
      </Modal>
    </div>
  );
}

function Row({
  icon, label, value, last,
}: {
  icon: React.ReactNode; label: string; value: string; last?: boolean;
}) {
  return (
    <div
      style={{
        padding: "14px 16px",
        display: "flex",
        alignItems: "center",
        gap: 12,
        borderBottom: last ? "none" : "0.5px solid #E5DDCB",
      }}
    >
      <span style={{ color: "#2B2721", fontSize: 15 }}>{icon}</span>
      <span style={{ fontSize: 13, color: "#5C5547" }}>{label}</span>
      <span style={{ marginLeft: "auto", fontSize: 13, color: "#2B2721" }}>{value}</span>
    </div>
  );
}
