"use client";

import {
  MutationCache,
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { useState } from "react";
import { AntdRegistry } from "@ant-design/nextjs-registry";
import { App as AntdApp, ConfigProvider, message } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import { tokens } from "@/lib/design-tokens";
import { extractErrorMessage } from "@/lib/api";

// 全局 dayjs 中文 locale —— AntD ConfigProvider 的 zhCN 不控制 DatePicker 内部的
// 月份/周名（那部分走 dayjs locale），所以必须在这里也设一次，否则月份面板会 fallback 英文。
dayjs.locale("zh-cn");

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        // 全局错误兜底：任何 mutation 失败都弹 toast，避免"按钮按了没反应"。
        // mutation 的 onError 仍可定制特定文案，但即使没写也至少会显示通用错误。
        mutationCache: new MutationCache({
          onError: (error, _variables, _context, mutation) => {
            // meta.silent = true 的 mutation 自己处理（如静默重试）。
            if (mutation.meta?.silent) return;
            message.error(extractErrorMessage(error));
          },
        }),
        // 查询失败的兜底：仅在已经有过数据后再失败时弹（避免首次加载未登录就刷一堆 toast）。
        queryCache: new QueryCache({
          onError: (error, query) => {
            if (query.meta?.silent) return;
            if (query.state.data !== undefined) {
              message.error(extractErrorMessage(error));
            }
          },
        }),
        defaultOptions: {
          queries: {
            // Keep cache warm for 30s — long enough that quickly toggling between
            // pages reuses data (no flash of loading), short enough that returning
            // to a list after a coffee break shows fresh state.
            staleTime: 30 * 1000,
            gcTime: 30 * 60 * 1000,
            retry: 1,
            refetchOnWindowFocus: false,
            // Refetch when a component remounts AND its data is stale. Previous
            // `false` made navigating back to a page show stale data forever
            // (until manually invalidated), which felt broken to users.
            refetchOnMount: true,
            refetchOnReconnect: "always",
          },
        },
      })
  );

  return (
    <AntdRegistry>
      <ConfigProvider
        locale={zhCN}
        theme={{
          token: {
            // 品牌主色板
            colorPrimary: "#2B2721",
            colorPrimaryHover: "#1A1612",
            colorPrimaryActive: "#1A1612",
            colorSuccess: "#7B8578",
            colorWarning: "#8A6E5A",
            colorError: "#8A6E5A",
            colorInfo: "#6B665B",
            colorBgLayout: "#FBF8F1",
            colorBgContainer: "#FBF8F1",
            colorBgElevated: "#FBF8F1",
            colorBorder: "#E5DDCB",
            colorBorderSecondary: "#E5DDCB",
            colorText: "#2B2721",
            colorTextSecondary: "#5C5547",
            colorTextTertiary: "#7A6F5F",
            // 圆角
            borderRadius: 12,
            borderRadiusLG: 16,
            borderRadiusSM: 8,
            borderRadiusXS: 4,
            // 字体
            fontFamily: tokens.font.family,
            fontSize: tokens.font.size.base,
            fontSizeLG: tokens.font.size.lg,
            fontSizeSM: tokens.font.size.sm,
            fontSizeXL: tokens.font.size.xl,
            fontSizeHeading1: tokens.font.size["3xl"],
            fontSizeHeading2: tokens.font.size["2xl"],
            fontSizeHeading3: tokens.font.size.xl,
            fontSizeHeading4: tokens.font.size.lg,
            fontWeightStrong: 500,
            lineHeight: 1.6,
            // 阴影清零
            boxShadow: "none",
            boxShadowSecondary: "none",
            boxShadowTertiary: "none",
            // 控件
            controlHeight: 36,
            controlHeightLG: 40,
            controlHeightSM: 28,
            controlOutline: "transparent",
            // 动效
            motionDurationFast: "160ms",
            motionDurationMid: "240ms",
            motionDurationSlow: "420ms",
            wireframe: false,
          },
          components: {
            Layout: {
              siderBg: "#FBF8F1",
              headerBg: "#FBF8F1",
              bodyBg: "#FBF8F1",
              triggerBg: "#F5F1EA",
              triggerColor: "#5C5547",
              headerHeight: tokens.layout.headerHeight,
              headerPadding: `0 ${tokens.space[5]}px`,
            },
            Menu: {
              itemBg: "transparent",
              subMenuItemBg: "transparent",
              itemSelectedBg: "#F5F1EA",
              itemSelectedColor: "#2B2721",
              itemHoverBg: "#F5F1EA",
              itemHoverColor: "#2B2721",
              itemColor: "#5C5547",
              itemBorderRadius: 12,
              itemHeight: 36,
              itemMarginInline: 8,
              itemMarginBlock: 2,
              iconSize: 16,
              collapsedIconSize: 18,
            },
            Button: {
              borderRadius: 999,
              borderRadiusLG: 999,
              borderRadiusSM: 999,
              controlHeight: 36,
              controlHeightLG: 40,
              controlHeightSM: 28,
              fontWeight: 500,
              primaryShadow: "none",
              defaultShadow: "none",
              dangerShadow: "none",
              paddingInline: 20,
            },
            Table: {
              headerBg: "#F5F1EA",
              headerColor: "#5C5547",
              headerBorderRadius: 0,
              headerSplitColor: "transparent",
              rowHoverBg: "#F5F1EA",
              borderColor: "#E5DDCB",
              cellPaddingBlock: 14,
              cellPaddingInline: 16,
              cellFontSize: tokens.font.size.base,
              footerBg: "#F5F1EA",
            },
            Card: {
              paddingLG: 20,
              headerBg: "transparent",
              actionsBg: "transparent",
              borderRadiusLG: 12,
              boxShadowTertiary: "none",
            },
            Input: {
              controlHeight: 36,
              controlHeightLG: 40,
              borderRadius: 8,
              activeBorderColor: "#2B2721",
              hoverBorderColor: "#5C5547",
              activeShadow: "0 0 0 2px rgba(43,39,33,.08)",
            },
            Select: {
              controlHeight: 36,
              controlHeightLG: 40,
              borderRadius: 8,
              optionSelectedBg: "#F5F1EA",
              optionSelectedColor: "#2B2721",
            },
            DatePicker: {
              controlHeight: 36,
              controlHeightLG: 40,
              borderRadius: 8,
              activeBorderColor: "#2B2721",
              hoverBorderColor: "#5C5547",
            },
            Tag: {
              defaultBg: "#F5F1EA",
              defaultColor: "#5C5547",
              borderRadiusSM: 999,
              fontSizeSM: tokens.font.size.xs,
            },
            Modal: {
              borderRadiusLG: 16,
              contentBg: "#FBF8F1",
              headerBg: "#FBF8F1",
              paddingContentHorizontalLG: 24,
              titleFontSize: tokens.font.size.xl,
              titleLineHeight: 1.4,
            },
            Drawer: {
              colorBgElevated: "#FBF8F1",
              paddingLG: 24,
            },
            Tabs: {
              horizontalItemPadding: "10px 0",
              horizontalItemGutter: 28,
              titleFontSize: tokens.font.size.base,
              itemSelectedColor: "#2B2721",
              itemHoverColor: "#2B2721",
              itemColor: "#5C5547",
              inkBarColor: "#2B2721",
            },
            Segmented: {
              itemSelectedBg: "#FBF8F1",
              itemSelectedColor: "#2B2721",
              itemColor: "#5C5547",
              itemHoverColor: "#2B2721",
              trackBg: "#F5F1EA",
              trackPadding: 3,
              controlHeight: 32,
              controlHeightLG: 36,
            },
            Statistic: {
              titleFontSize: tokens.font.size.sm,
              contentFontSize: tokens.font.size["3xl"],
            },
            Dropdown: {
              controlItemBgHover: "#F5F1EA",
              controlItemBgActive: "#F5F1EA",
              controlItemBgActiveHover: "#E5DDCB",
              borderRadiusLG: 16,
            },
            Badge: {
              dotSize: 8,
              fontSizeSM: tokens.font.size.xs,
            },
            Tooltip: {
              colorBgSpotlight: "#2B2721",
              borderRadius: 8,
            },
            Message: {
              contentPadding: "10px 16px",
            },
            Form: {
              itemMarginBottom: 20,
              labelColor: "#2B2721",
              labelFontSize: tokens.font.size.base,
              verticalLabelPadding: "0 0 6px",
            },
            Divider: {
              colorSplit: "#E5DDCB",
            },
            Empty: {
              colorTextDisabled: "#7A6F5F",
            },
          },
        }}
      >
        <QueryClientProvider client={queryClient}>
          <AntdApp>{children}</AntdApp>
        </QueryClientProvider>
      </ConfigProvider>
    </AntdRegistry>
  );
}
