// 业主结算单术语解释 + 分成比例格式化。
// 文案口径以 backend/app/api/v1/settlements.py 结算生成逻辑为准
// (v2#4 公式: 实际打款 = Σ房间(净收入 × 分成比例) − 业主承担支出)——
// 改公式时这里的解释必须同步改,否则解释比不解释更糟。

export const SETTLEMENT_TERM_TIPS: Record<string, string> = {
  actual_owner_amount:
    "本月最终打给您的金额 = 每套房的(净收入 × 分成比例)加总,再减去您承担的支出。",
  total_net_revenue:
    "所有房间的房费收入,减去携程/美团等平台佣金后的金额(如有平台补贴也计入)。",
  deducted_expenses:
    "按约定由您承担的费用(如布草、维修、耗品等),已按每类费用的分摊比例计算。",
  room_owner_net:
    "这套房您的净得 = 净收入(房费−平台佣金) × 分成比例 − 您承担的支出。",
  share_ratio:
    "生成本结算单时约定的分成比例快照,之后改比例不影响已生成的结算单。",
};

// "0.7"/"0.70"/0.7 → "70%";异常/缺省值返回 null(调用方直接不显示,别渲染出 NaN%)。
export function formatShareRatio(ratio: string | number | null | undefined): string | null {
  if (ratio === null || ratio === undefined || ratio === "") return null;
  const v = typeof ratio === "string" ? parseFloat(ratio) : ratio;
  if (Number.isNaN(v) || v <= 0 || v > 1) return null;
  const pct = v * 100;
  // 70 → "70%", 67.5 → "67.5%" (最多 1 位小数,再多是配置错误按 1 位截断显示)
  const rounded = Math.round(pct * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded}%`;
}
