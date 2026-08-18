"use client";

import React from "react";
import { Input as AntInput } from "antd";
import type { InputProps, InputRef } from "antd";

type MobileInputProps = InputProps;

export const MobileInput = React.forwardRef<InputRef, MobileInputProps>(
  ({ style, ...rest }, ref) => {
    return (
      <AntInput
        ref={ref}
        style={{ fontSize: 16, ...(style || {}) }}
        {...rest}
      />
    );
  }
);

MobileInput.displayName = "MobileInput";
