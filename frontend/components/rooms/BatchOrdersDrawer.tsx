"use client";

import React from "react";
import { Drawer } from "antd";
import { useIsMobile } from "@/lib/responsive";
import { tokens } from "@/lib/design-tokens";
import OrdersPage from "@/app/(dashboard)/orders/page";

interface Props {
  open: boolean;
  onClose: () => void;
}

/**
 * 「筛选 / 批量 / 导出」抽屉。
 *
 * 直接复用 /orders 页面整体作为抽屉内容 —— 这样筛选/批量/导出/分页等所有
 * 重度运营功能 1:1 保留，不需要二次开发。/orders 路由仍可深链访问，被通知
 * 邮件/旧收藏夹引用时也不会 404；这里把它叠在抽屉里只是新的入口形态。
 *
 * 注意：抽屉内点「新建订单」会 router.push 到 /orders/new（全屏跳转），
 * 这是有意保留的：深度新建场景下用全屏表单更舒适，简单新建走运营台
 * 顶栏的 QuickCreateOrderModal。
 */
export function BatchOrdersDrawer({ open, onClose }: Props) {
  const isMobile = useIsMobile();

  return (
    <Drawer
      open={open}
      onClose={onClose}
      placement="right"
      title="订单查询 · 批量管理"
      width={isMobile ? "100%" : "80vw"}
      destroyOnHidden
      styles={{ body: { padding: isMobile ? 12 : 20, background: tokens.color.bg.page } }}
    >
      <OrdersPage />
    </Drawer>
  );
}
