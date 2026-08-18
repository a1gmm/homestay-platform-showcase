import type { AxiosError } from "axios";

// 把任意 axios 错误归一成一句中文给用户看。
// 独立成模块：owner/staff/booking 子端也要用，不必拖上 admin 的 axios 实例。
//
// 重要：FastAPI 422 的 detail 是对象数组，绝对不能直接塞给 message.error
// （React 渲染对象数组会崩溃，用户什么提示都看不到 —— 2026-06-12 生产事故）。
// 所有错误提示必须经过这个函数。
//
// fallback：调用方的业务文案（如「排房失败」），仅在没有 detail 且不属于
// 已知 status 归类时使用。
// 建单同客同日期重复拦截（后端 409 + 响应头 X-Error-Code: duplicate_order）。
// 两个建单入口据此弹「确认后允许重复」的对话框，而不是裸报错。
export function isDuplicateOrderError(error: unknown): boolean {
  const ax = error as AxiosError;
  return (
    ax?.response?.status === 409 &&
    String(ax.response.headers?.["x-error-code"] ?? "") === "duplicate_order"
  );
}

export function extractErrorMessage(error: unknown, fallback?: string): string {
  const ax = error as AxiosError<{ detail?: string | Array<{ msg?: string }> }>;
  if (ax?.code === "ECONNABORTED") {
    return "请求超时，可能是后端冷启动或网络抖动，请稍后重试";
  }
  if (ax && !ax.response) {
    return "网络异常，请检查网络后重试";
  }
  const detail = ax?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  // pydantic 自定义校验消息带 "Value error, " 前缀，剥掉只留中文
  if (Array.isArray(detail) && detail[0]?.msg)
    return String(detail[0].msg).replace(/^Value error, /, "");
  const status = ax?.response?.status;
  if (status === 401) return "登录已过期，请重新登录";
  if (status === 403) return "无权进行此操作";
  if (status === 404) return "请求的资源不存在";
  if (status && status >= 500) return "服务器错误，请稍后重试";
  return fallback || ax?.message || "操作失败";
}
