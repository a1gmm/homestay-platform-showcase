"use client";

import { DatePicker } from "antd";
import type { RangePickerProps } from "antd/es/date-picker";
import { tokens } from "@/lib/design-tokens";

type Props = RangePickerProps & {
  label: string;
  help?: string;
};

/**
 * 统一日期控件的表达层：业务标签和口径说明选完日期后也一直可见。
 * 具体按入住、退房、发生或操作日期计算，仍由调用页面显式传入，避免万能组件藏政策。
 */
export function BusinessDateRangeField({ label, help, style, ...pickerProps }: Props) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
      <span style={{ color: tokens.color.text.secondary, fontSize: 12, lineHeight: 1.3 }}>
        {label}
      </span>
      <DatePicker.RangePicker
        placeholder={["开始日期", "结束日期"]}
        {...pickerProps}
        style={{ width: "100%", ...style }}
      />
      {help ? (
        <span style={{ color: tokens.color.text.tertiary, fontSize: 11, lineHeight: 1.3 }}>
          {help}
        </span>
      ) : null}
    </div>
  );
}
