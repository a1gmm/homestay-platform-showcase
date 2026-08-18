import React, { type ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render as rtlRender } from "@testing-library/react";

// 组件测试统一入口：带 QueryClientProvider，关闭重试让失败立即可断言
export function renderWithQuery(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return { qc, ...rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>) };
}

// 别名：部分测试直接 `render(<Page />)`，等价于 renderWithQuery，语义上更贴近
// @testing-library/react 的默认写法。同时透传 screen/waitFor 等常用查询工具，
// 让消费方只需从 "@/test/utils" 一处导入，不用再拼 "@testing-library/react"。
export const render = renderWithQuery;
export * from "@testing-library/react";
