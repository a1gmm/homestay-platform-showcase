"use client";

import React, { useEffect } from "react";
import {
  Modal,
  Form,
  Row,
  Col,
  Select,
  Switch,
  Input,
  Button,
  Space,
  Typography,
} from "antd";
import { MobileInputNumber } from "@/components/ui/MobileInputNumber";
import { MobileDateTimePicker } from "@/components/ui/MobileDateTimePicker";
import { DollarOutlined, EditOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { useIsMobile } from "@/lib/responsive";

const { Text } = Typography;

const PAYMENT_METHODS = [
  { value: "wechat", label: "微信支付" },
  { value: "alipay", label: "支付宝" },
  { value: "cash", label: "现金" },
  { value: "bank_transfer", label: "银行转账" },
  { value: "platform", label: "平台代收" },
  { value: "other", label: "其他" },
];

interface Props {
  open: boolean;
  order: any;
  onClose: () => void;
  onFinish: (values: any) => void;
  loading: boolean;
  // 编辑模式：传入待编辑的 payment 记录。提交时 onFinish 只收到 amount/method/notes。
  payment?: any | null;
  // 现有收款记录，用于计算"已收/待收"并做超收校验。
  payments?: any[];
  // 传入即显示「保存并再记一笔」按钮（定金+尾款一口气录完，不用重开弹窗）。
  // 调用方在 mutation 成功后调 resetForNext() 清空金额/备注，弹窗保持打开。
  onFinishAndNext?: (values: any, resetForNext: () => void) => void;
}

export default function PaymentModal({
  open,
  order,
  onClose,
  onFinish,
  loading,
  payment,
  payments = [],
  onFinishAndNext,
}: Props) {
  const isMobile = useIsMobile();
  const [form] = Form.useForm();
  const isEdit = !!payment;

  // 房费已收：排除押金；编辑模式排除本条（编辑就是覆写，不能重复计）
  const actualPrice = Number(order?.actual_price ?? 0);
  const houseFeePaid = (payments || [])
    .filter((p) => !p.is_deposit && (!isEdit || p.payment_id !== payment?.payment_id))
    .reduce((s, p) => s + Number(p.amount), 0);
  const houseFeeRemaining = Math.max(0, actualPrice - houseFeePaid);

  const buildCreatePayload = (values: any) => ({
    order_id: order.order_id,
    amount: values.amount,
    method: values.method,
    paid_at: values.paid_at?.toISOString() || new Date().toISOString(),
    is_deposit: values.is_deposit || false,
    notes: values.notes || null,
  });

  // 「保存并再记一笔」：校验通过后交给调用方 mutate；成功后调用方用 resetForNext
  // 清空金额/备注/押金开关（收款方式和时间保留——连续录定金+尾款通常同一渠道）。
  const handleSaveAndNext = async () => {
    if (!onFinishAndNext) return;
    const values = await form.validateFields();
    onFinishAndNext(buildCreatePayload(values), () => {
      form.setFieldsValue({ amount: undefined, notes: "", is_deposit: false });
    });
  };

  useEffect(() => {
    if (!open) return;
    if (isEdit) {
      form.setFieldsValue({
        amount: Number(payment.amount),
        method: payment.method,
        paid_at: dayjs(payment.paid_at),
        is_deposit: !!payment.is_deposit,
        notes: payment.notes || "",
      });
    } else {
      form.resetFields();
    }
  }, [open, isEdit, payment, form]);

  return (
    <Modal
      open={open}
      onCancel={onClose}
      // 移动端用全宽 + 顶部小留白，避免默认 520px 在 360-414px 屏宽横向溢出
      width={isMobile ? "calc(100vw - 16px)" : 520}
      style={isMobile ? { top: 12, paddingBottom: 0, maxWidth: "100vw" } : undefined}
      title={
        <Space>
          {isEdit ? (
            <EditOutlined style={{ color: "#1677ff" }} />
          ) : (
            <DollarOutlined style={{ color: "#52c41a" }} />
          )}
          <Text strong>{isEdit ? "修改收款" : "记录收款"}</Text>
        </Space>
      }
      footer={null}
      destroyOnHidden
    >
      {order && (
        <div
          style={{
            marginBottom: 12,
            padding: "10px 12px",
            background: "#FAFAF7",
            border: "1px solid #EEE9DC",
            borderRadius: 8,
          }}
        >
          <div style={{ fontSize: 13, color: "#333", marginBottom: 6 }}>
            {order.guest_name} · {order.check_in_date}（{order.nights}晚）
          </div>
          <div style={{ display: "flex", gap: 18, fontSize: 13, flexWrap: "wrap" }}>
            <span>
              应收&nbsp;
              <span className="tabular" style={{ fontWeight: 600 }}>
                ¥{actualPrice.toLocaleString()}
              </span>
            </span>
            <span>
              已收房费&nbsp;
              <span className="tabular" style={{ fontWeight: 600, color: "#52c41a" }}>
                ¥{houseFeePaid.toLocaleString()}
              </span>
            </span>
            <span>
              {isEdit ? "本条之外待收" : "待收"}&nbsp;
              <span
                className="tabular"
                style={{
                  fontWeight: 700,
                  color: houseFeeRemaining > 0 ? "#ff4d4f" : "#52c41a",
                }}
              >
                ¥{houseFeeRemaining.toLocaleString()}
              </span>
            </span>
          </div>
        </div>
      )}
      <Form
        form={form}
        layout="vertical"
        onFinish={(values) => {
          if (isEdit) {
            // 编辑只送 amount/method/notes —— paid_at 和 created_by 保留原值，
            // 避免审计追溯被篡改。
            onFinish({
              payment_id: payment.payment_id,
              amount: values.amount,
              method: values.method,
              notes: values.notes || null,
            });
          } else {
            onFinish(buildCreatePayload(values));
          }
        }}
        initialValues={
          isEdit
            ? undefined
            : { method: "wechat", is_deposit: false, paid_at: dayjs() }
        }
      >
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="amount"
              label={
                <span>
                  <span style={{ color: "#ff4d4f", marginRight: 4 }}>*</span>
                  收款金额 (¥)
                </span>
              }
              required
              rules={[
                { required: true, message: "收款金额必填，请输入大于 0 的金额" },
                {
                  validator: (_, v) => {
                    if (v === undefined || v === null) return Promise.resolve();
                    const n = Number(v);
                    if (!(n > 0)) return Promise.reject(new Error("金额必须大于 0"));
                    // 仅对房费做超收校验；押金允许超过 actual_price
                    const isDeposit = !!form.getFieldValue("is_deposit");
                    if (!isDeposit && actualPrice > 0 && n > houseFeeRemaining + 0.005) {
                      return Promise.reject(
                        new Error(`房费最多还能收 ¥${houseFeeRemaining.toLocaleString()}`)
                      );
                    }
                    return Promise.resolve();
                  },
                },
              ]}
            >
              <MobileInputNumber
                style={{ width: "100%" }}
                size="large"
                min={0.01}
                precision={2}
                prefix="¥"
                placeholder={
                  houseFeeRemaining > 0 ? `最多 ¥${houseFeeRemaining.toLocaleString()}` : "必填，大于 0"
                }
                inputMode="decimal"
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="method"
              label="收款方式"
              rules={[{ required: true }]}
            >
              <Select size="large" options={PAYMENT_METHODS} />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={16}>
          {/* 移动端整行：日期+时间双控件并排需要全宽，挤在 14/24 里放不下 */}
          <Col span={isMobile ? 24 : 14}>
            <Form.Item
              name="paid_at"
              label="收款时间"
              rules={isEdit ? [] : [{ required: true }]}
            >
              <MobileDateTimePicker
                style={{ width: "100%" }}
                size="large"
                disabled={isEdit}
              />
            </Form.Item>
          </Col>
          <Col span={isMobile ? 24 : 10}>
            <Form.Item
              name="is_deposit"
              label="是押金？"
              valuePropName="checked"
            >
              <Switch
                checkedChildren="押金"
                unCheckedChildren="房费"
                disabled={isEdit}
              />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="notes" label="备注（选填）">
          <Input placeholder="如: 微信转账截图已保存" />
        </Form.Item>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}>
          <Button onClick={onClose}>取消</Button>
          {!isEdit && onFinishAndNext && (
            <Button loading={loading} onClick={handleSaveAndNext}>
              保存,再记一笔
            </Button>
          )}
          <Button
            type="primary"
            htmlType="submit"
            loading={loading}
            icon={isEdit ? <EditOutlined /> : <DollarOutlined />}
          >
            {isEdit ? "保存修改" : "确认收款"}
          </Button>
        </div>
      </Form>
    </Modal>
  );
}
