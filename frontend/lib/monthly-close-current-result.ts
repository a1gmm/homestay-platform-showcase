import type { AssistantReply, MonthlyCloseProjection } from "./monthly-close";

/** An old read must not compete with the current source state or remain actionable. */
export function recognitionChanged(reply: AssistantReply, projection: MonthlyCloseProjection): boolean {
  if (reply.tool !== "financial_case" || reply.facts.ledger_scope || reply.facts.order_scope
    || reply.facts.billing_month !== projection.billing_month || !Array.isArray(reply.facts.sources)) return false;
  return reply.facts.sources.some((source: { source_id: string; fact_count: number }) => {
    const current = projection.financial_case?.sources.find((item) => item.source_id === source.source_id);
    return current && current.fact_count !== source.fact_count;
  });
}

export function conversationTurns(replies: AssistantReply[], exchanges: Array<{ assistantReply?: AssistantReply }>): AssistantReply[] {
  const turns = [...replies];
  for (const exchange of exchanges) if (exchange.assistantReply && !turns.some((reply) => reply.run_id === exchange.assistantReply?.run_id)) turns.push(exchange.assistantReply);
  return turns;
}
