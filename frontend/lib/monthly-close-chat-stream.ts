import type { AssistantReply } from "./monthly-close";

export type AssistantProgress = { stage: "received" | "understanding" | "checking" | "planning" | "completed"; message: string };
const stages = new Set(["received", "understanding", "checking", "planning", "completed"]);

/** XHR supplies cumulative responseText. Consume complete frames exactly once. */
export function createChatStreamReader(onProgress: (progress: AssistantProgress) => void) {
  let consumed = 0;
  let result: AssistantReply | undefined;
  let failure: string | undefined;
  return {
    accept(text: string) {
      let end: number;
      while ((end = text.indexOf("\n\n", consumed)) >= 0) {
        const frame = text.slice(consumed, end);
        consumed = end + 2;
        const event = frame.split("\n").find((line) => line.startsWith("event: "))?.slice(7);
        const data = frame.split("\n").filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n");
        if (!event || !data) continue;
        const payload = JSON.parse(data);
        if (event === "progress" && stages.has(payload.stage) && typeof payload.message === "string") onProgress(payload as AssistantProgress);
        if (event === "result") result = payload as AssistantReply;
        if (event === "error") failure = typeof payload.message === "string" ? payload.message : "请求未完成";
      }
    },
    finish() {
      if (failure) throw new Error(failure);
      if (!result?.run_id) throw new Error("连接中断，请刷新查看已保存的结果后再重试");
      return result;
    },
  };
}
