import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { message } from "antd";
import { ordersApi, financeApi } from "@/lib/api";
import { invalidateOrderRelated, invalidatePaymentRelated } from "@/lib/order-cache";
import type { OrderStatus, PaymentCreate } from "@/lib/types";

export interface OrderFilters {
  status?: string; // 单值或逗号分隔多值(今日清单卡跳转用)，后端 in_ 过滤
  channel?: string;
  keyword?: string;
  check_in_from?: string;
  check_in_to?: string;
  check_out_from?: string;
  check_out_to?: string;
}

export function useOrders(filters: OrderFilters = {}, page: number = 1) {
  const queryClient = useQueryClient();

  // 订单页按「段」分页：一个续住组一行（合计/晚数/房间序列后端算好，前端不归并）。
  // ⚠️ 这一行就是回滚开关：换回 ordersApi.list 即恢复按单列表，后端不用碰、不用重部。
  // KpiPeekDrawer 与 NewOrderWatcher 仍调 ordersApi.list —— 它们要的就是按单语义，别跟着改。
  const ordersQuery = useQuery({
    queryKey: ["orders", filters, page],
    queryFn: () =>
      ordersApi
        .listBySegment({ ...filters, page, page_size: 20 })
        .then((r) => r.data),
    placeholderData: keepPreviousData,
  });

  const transitionMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: OrderStatus }) =>
      ordersApi.transition(id, status),
    onSuccess: () => {
      message.success("订单状态已更新");
      invalidateOrderRelated(queryClient);
    },
  });

  const cancelMutation = useMutation({
    mutationFn: (id: string) => ordersApi.cancel(id),
    onSuccess: () => {
      message.success("订单已取消");
      invalidateOrderRelated(queryClient);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => ordersApi.delete(id),
    onSuccess: () => {
      message.success("订单已删除");
      invalidateOrderRelated(queryClient);
    },
  });

  const usePaymentsQuery = (orderId?: string, enabled?: boolean) =>
    useQuery({
      queryKey: ["payments", orderId],
      queryFn: () =>
        financeApi.payments.list(orderId).then((r) => r.data),
      enabled: !!orderId && !!enabled,
      staleTime: 5 * 60 * 1000,
    });

  const createPaymentMutation = useMutation({
    mutationFn: (data: PaymentCreate) => financeApi.payments.create(data),
    onSuccess: () => {
      message.success("收款已记录");
      invalidatePaymentRelated(queryClient);
    },
  });

  return {
    ordersQuery,
    transitionMutation,
    cancelMutation,
    deleteMutation,
    usePaymentsQuery,
    createPaymentMutation,
  };
}

