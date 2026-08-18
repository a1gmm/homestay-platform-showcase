"use client";

import { useState } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { Modal, Form, Input, message } from "antd";
import { LockOutlined } from "@ant-design/icons";
import { authApi } from "@/lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

interface FormValues {
  current_password: string;
  new_password: string;
  confirm_password: string;
}

export function ChangePasswordModal({ open, onClose, onSuccess }: Props) {
  const [form] = Form.useForm<FormValues>();
  const [submitting, setSubmitting] = useState(false);

  const handleClose = () => {
    if (submitting) return;
    form.resetFields();
    onClose();
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setSubmitting(true);
      await authApi.changePassword(values.current_password, values.new_password);
      message.success("密码修改成功，请使用新密码重新登录");
      form.resetFields();
      onClose();
      // 强制重新登录,避免新旧 token 状态不一致
      onSuccess();
    } catch (err: unknown) {
      // antd Form.validateFields 失败时不提示(只是字段验证,errorFields 是其特征)
      if (!(err && typeof err === "object" && "errorFields" in err)) {
        message.error(extractErrorMessage(err, "密码修改失败，请稍后重试"));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="修改密码"
      open={open}
      onOk={handleSubmit}
      onCancel={handleClose}
      okText="确认修改"
      cancelText="取消"
      confirmLoading={submitting}
      maskClosable={false}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" autoComplete="off" style={{ marginTop: 12 }}>
        <Form.Item
          label="当前密码"
          name="current_password"
          rules={[{ required: true, message: "请输入当前密码" }]}
        >
          <Input.Password
            prefix={<LockOutlined />}
            placeholder="当前登录密码"
            autoComplete="current-password"
          />
        </Form.Item>
        <Form.Item
          label="新密码"
          name="new_password"
          rules={[
            { required: true, message: "请输入新密码" },
            { min: 8, message: "密码长度不能少于 8 位" },
            {
              validator: (_, value: string) => {
                if (!value) return Promise.resolve();
                const hasLetter = /[a-zA-Z]/.test(value);
                const hasNumber = /\d/.test(value);
                if (!hasLetter || !hasNumber) {
                  return Promise.reject(new Error("密码必须同时包含字母和数字"));
                }
                return Promise.resolve();
              },
            },
          ]}
        >
          <Input.Password
            prefix={<LockOutlined />}
            placeholder="≥8 位,字母 + 数字"
            autoComplete="new-password"
          />
        </Form.Item>
        <Form.Item
          label="确认新密码"
          name="confirm_password"
          dependencies={["new_password"]}
          rules={[
            { required: true, message: "请再次输入新密码" },
            ({ getFieldValue }) => ({
              validator(_, value: string) {
                if (!value || getFieldValue("new_password") === value) {
                  return Promise.resolve();
                }
                return Promise.reject(new Error("两次输入的密码不一致"));
              },
            }),
          ]}
        >
          <Input.Password
            prefix={<LockOutlined />}
            placeholder="再次输入新密码"
            autoComplete="new-password"
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
