import type { AssistantReply, MonthlyCloseProjection } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";

export function ConversationMemoryReply({ reply, projection }: { reply: AssistantReply; projection: MonthlyCloseProjection }) {
  if (reply.tool !== "conversation_memory" || projection.actor_role !== "admin" || reply.facts.billing_month !== projection.billing_month || !Array.isArray(reply.facts.notes)) return null;
  const notes = reply.facts.notes.filter((note): note is { topic: string; text: string } => note && typeof note === "object" && typeof note.topic === "string" && typeof note.text === "string");
  if (!notes.length) return null;
  return <div aria-label="本月对话备注" style={{ marginTop: 12, display: "grid", gap: 8, overflowWrap: "anywhere" }}>
    <div style={{ color: tokens.anyu.color.stone }}>本月对话备注</div>
    {notes.map((note, index) => <div key={`${note.topic}-${index}`} style={{ whiteSpace: "pre-wrap" }}>{note.text}</div>)}
  </div>;
}
