"use client";

import React from "react";
import { DatePicker, TimePicker } from "antd";
import dayjs, { Dayjs } from "dayjs";
import { useIsMobile } from "@/lib/responsive";
import { mergePickedDate, mergePickedTime } from "@/lib/datetime-field";

interface Props {
  value?: Dayjs | null;
  onChange?: (v: Dayjs | null) => void;
  disabled?: boolean;
  // 移动端务必传 "large"：antd picker 对内层 input 显式设 font-size（默认 14px），
  // 外层 style 的字号继承不进去；large 尺寸 input 才是 16px（CLAUDE.md 防 iOS 缩放线）。
  size?: "small" | "middle" | "large";
  style?: React.CSSProperties;
  // Form.Item 会给子控件注入 id（label htmlFor / scrollToField 靠它找元素）。
  // 拆成两个控件后挂到日期框上——它是这个字段的"主"输入。
  id?: string;
}

/**
 * 日期时间选择器：桌面 = 单 DatePicker+showTime；移动 = 日期 + 时间两个控件并排。
 *
 * 单 DatePicker+showTime 的弹层（288px 日期面板 + 时间列 ≈ 443px）在 375px 视口
 * 必然溢出：rc-trigger bottomRight 对齐时左缘 -168px，左侧日历和「上月/上年」
 * 完全不可用；套居中（RangePicker 那套 CSS）则两边同时被裁。拆成两个控件后
 * 日期面板 288px、时间弹层 ~130px 各自都放得下，无需再动全局 CSS。
 *
 * 受控值仍是单个 dayjs（Form.Item 直接包），合并逻辑见 lib/datetime-field.ts。
 * 移动端 inputReadOnly：值都从面板选，不弹软键盘。时间列 needConfirm=false：
 * 点小时/分钟立即生效——默认要点「确定」，手机上点到弹层外就静默丢掉所选时间，
 * 恰是本组件要消灭的"错时间戳"故障类。
 */
export function MobileDateTimePicker({ value, onChange, disabled, size, style, id }: Props) {
  const isMobile = useIsMobile();

  if (!isMobile) {
    return (
      <DatePicker
        id={id}
        showTime
        format="YYYY-MM-DD HH:mm"
        value={value}
        onChange={(v) => onChange?.(v)}
        disabled={disabled}
        size={size}
        style={style}
      />
    );
  }

  return (
    <div style={{ display: "flex", gap: 8, ...(style || {}) }}>
      <DatePicker
        id={id}
        format="YYYY-MM-DD"
        value={value}
        onChange={(d) => onChange?.(mergePickedDate(value, d, dayjs()))}
        disabled={disabled}
        size={size}
        inputReadOnly
        style={{ flex: "1 1 60%", minWidth: 0 }}
      />
      <TimePicker
        format="HH:mm"
        value={value}
        onChange={(t) => onChange?.(mergePickedTime(value, t, dayjs()))}
        disabled={disabled}
        size={size}
        inputReadOnly
        allowClear={false}
        needConfirm={false}
        style={{ flex: "1 1 40%", minWidth: 0 }}
      />
    </div>
  );
}
