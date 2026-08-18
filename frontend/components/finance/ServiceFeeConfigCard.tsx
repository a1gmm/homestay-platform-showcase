"use client";

import React, { useEffect } from "react";
import {
  Card, Form, InputNumber, Button, Space, Typography, Skeleton, message, Alert,
} from "antd";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { financeApi, ServiceFeeConfigUpdate } from "@/lib/api";

const { Text, Paragraph } = Typography;

/** 财务页「费用标准设置」：业主服务费费率（保洁/续住/洗涤/日耗）。仅 admin 可见。
 *  改动即时生效——下一单退房记账即按新价，历史已记账支出不变。 */
export default function ServiceFeeConfigCard() {
  const [form] = Form.useForm();
  const qc = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["service-fee-config"],
    queryFn: async () => (await financeApi.serviceFeeConfig.get()).data,
  });

  useEffect(() => {
    if (data) {
      form.setFieldsValue({
        checkout_cleaning_fee: Number(data.checkout_cleaning_fee),
        instay_cleaning_fee: Number(data.instay_cleaning_fee),
        laundry_fee_per_room: Number(data.laundry_fee_per_room),
        consumable_fee_per_room_night: Number(data.consumable_fee_per_room_night),
      });
    }
  }, [data, form]);

  const save = useMutation({
    mutationFn: (vals: ServiceFeeConfigUpdate) =>
      financeApi.serviceFeeConfig.update(vals),
    onSuccess: () => {
      message.success("费用标准已更新，下一单退房起按新价计费");
      qc.invalidateQueries({ queryKey: ["service-fee-config"] });
    },
    onError: () => message.error("保存失败，请重试"),
  });

  const items: { name: keyof ServiceFeeConfigUpdate; label: string; unit: string; hint: string }[] = [
    { name: "checkout_cleaning_fee", label: "退房保洁费", unit: "元/间/单", hint: "每间房退房打扫一次收取" },
    { name: "instay_cleaning_fee", label: "续住·日常保洁费", unit: "元/间/次", hint: "续住/在住登记打扫时收取" },
    { name: "laundry_fee_per_room", label: "洗涤费", unit: "元/间/单", hint: "每间每单一次（续住合并单只收一次）" },
    { name: "consumable_fee_per_room_night", label: "日耗品", unit: "元/间/夜", hint: "每间房每住一晚收取" },
  ];

  return (
    <Card title="费用标准设置" style={{ maxWidth: 560 }}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="这些费用在退房时自动从房东分成里扣（房东全担）"
        description="修改后即时生效——下一单退房起按新价计费，历史已记账的费用不受影响。"
      />
      <Skeleton loading={isLoading} active>
        <Form
          form={form}
          layout="horizontal"
          labelCol={{ span: 10 }}
          wrapperCol={{ span: 14 }}
          onFinish={(vals) => save.mutate(vals)}
        >
          {items.map((it) => (
            <Form.Item
              key={it.name}
              name={it.name}
              label={it.label}
              extra={<Text type="secondary" style={{ fontSize: 12 }}>{it.hint}</Text>}
              rules={[
                { required: true, message: "请填写" },
                {
                  type: "number",
                  min: 0,
                  message: "不能为负",
                },
              ]}
            >
              <InputNumber
                min={0}
                step={1}
                precision={2}
                inputMode="decimal"
                style={{ width: 160 }}
                addonAfter={it.unit}
              />
            </Form.Item>
          ))}
          <Form.Item wrapperCol={{ offset: 10, span: 14 }}>
            <Space>
              <Button type="primary" htmlType="submit" loading={save.isPending}>
                保存
              </Button>
              <Button onClick={() => data && form.setFieldsValue({
                checkout_cleaning_fee: Number(data.checkout_cleaning_fee),
                instay_cleaning_fee: Number(data.instay_cleaning_fee),
                laundry_fee_per_room: Number(data.laundry_fee_per_room),
                consumable_fee_per_room_night: Number(data.consumable_fee_per_room_night),
              })}>
                重置
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Skeleton>
      <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
        举例：一间房住 4 晚 → 退房时扣 保洁{Number(data?.checkout_cleaning_fee ?? 65)}
        {" + "}洗涤{Number(data?.laundry_fee_per_room ?? 15)}
        {" + "}日耗{Number(data?.consumable_fee_per_room_night ?? 22)}×4，全部从房东那 70% 里扣。
      </Paragraph>
    </Card>
  );
}
