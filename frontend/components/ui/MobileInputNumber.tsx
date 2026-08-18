"use client";

import React from "react";
import { InputNumber as AntInputNumber } from "antd";
import type { InputNumberProps } from "antd";

type MobileInputNumberProps = InputNumberProps;

/**
 * InputNumber 的移动端包装：镜像 MobileInput，强制 fontSize:16 防 iOS 聚焦缩放。
 * CLAUDE.md 强制规则「所有 Input 必须 ≥16px」。金额类数字框（客人价 / 实收 / 押金 /
 * 佣金）原先用裸 Ant InputNumber（默认 ~14px），iOS 聚焦会触发整页缩放。
 * fontSize 设在外层 .ant-input-number，内层 input 无显式 font-size 故继承到 16。
 */
export const MobileInputNumber = React.forwardRef<
  React.ElementRef<typeof AntInputNumber>,
  MobileInputNumberProps
>(({ style, ...rest }, ref) => {
  return (
    <AntInputNumber
      ref={ref}
      style={{ fontSize: 16, ...(style || {}) }}
      {...rest}
    />
  );
});

MobileInputNumber.displayName = "MobileInputNumber";
