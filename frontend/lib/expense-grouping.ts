// 支出明细页：筛选 + 按订单折叠 的纯逻辑（从 finance/page.tsx 抽出以便单测）。
//
// 口径：
// - 先按 类别 / 房号 / 描述关键词 过滤（三者 AND，空则不限）。
// - 再把同一 order_id 的 ≥2 笔折叠成一个可展开父行（children 为原始明细，保持入参顺序）。
// - 单笔或无 order_id（公摊水电、手动录入等）保持平铺。
// - 顶层按代表日期（组内最大 expense_date）倒序，与后端 expense_date desc 一致。

export interface ExpenseLike {
  expense_id?: string;
  category?: string | null;
  amount?: number | string | null;
  description?: string | null;
  expense_date?: string | null;
  room_id?: string | null;
  order_id?: string | null;
  payer?: string | null;
  [k: string]: unknown;
}

export interface ExpenseFilters {
  category?: string;
  room?: string;
  search?: string;
}

export interface ExpenseGroupRow {
  __group: true;
  __key: string;
  order_id: string;
  amount: number;
  expense_date: string;
  __dateMin: string;
  __dateMax: string;
  __rooms: string[];
  __cats: string[];
  __payers: string[];
  __count: number;
  children: ExpenseLike[];
}

export type ExpenseRow = ExpenseLike | ExpenseGroupRow;

export function filterExpenses(
  expenses: ExpenseLike[] | undefined | null,
  filters: ExpenseFilters,
): ExpenseLike[] {
  const raw = Array.isArray(expenses) ? expenses : [];
  const kw = (filters.search ?? "").trim().toLowerCase();
  return raw.filter((e) => {
    if (filters.category && e.category !== filters.category) return false;
    if (filters.room && String(e.room_id ?? "") !== filters.room) return false;
    if (kw && !String(e.description ?? "").toLowerCase().includes(kw)) return false;
    return true;
  });
}

export function buildExpenseRows(
  expenses: ExpenseLike[] | undefined | null,
  filters: ExpenseFilters = {},
): ExpenseRow[] {
  const filtered = filterExpenses(expenses, filters);

  const byOrder = new Map<string, ExpenseLike[]>();
  const rows: ExpenseRow[] = [];
  for (const e of filtered) {
    if (e.order_id) {
      const arr = byOrder.get(e.order_id);
      if (arr) arr.push(e);
      else byOrder.set(e.order_id, [e]);
    } else {
      rows.push(e);
    }
  }

  Array.from(byOrder.entries()).forEach(([orderId, items]: [string, ExpenseLike[]]) => {
    if (items.length < 2) {
      rows.push(...items);
      return;
    }
    const dates = items.map((x) => String(x.expense_date ?? "")).sort();
    rows.push({
      __group: true,
      __key: `grp-${orderId}`,
      order_id: orderId,
      amount: items.reduce((s, x) => s + Number(x.amount || 0), 0),
      expense_date: dates[dates.length - 1],
      __dateMin: dates[0],
      __dateMax: dates[dates.length - 1],
      __rooms: Array.from(new Set(items.map((x) => x.room_id).filter(Boolean) as string[])),
      __cats: Array.from(new Set(items.map((x) => x.category).filter(Boolean) as string[])),
      __payers: Array.from(new Set(items.map((x) => x.payer).filter(Boolean) as string[])),
      __count: items.length,
      children: items,
    });
  });

  rows.sort((a, b) =>
    String((b as ExpenseRow).expense_date ?? "").localeCompare(String((a as ExpenseRow).expense_date ?? "")),
  );
  return rows;
}

export function isExpenseGroup(row: ExpenseRow): row is ExpenseGroupRow {
  return (row as ExpenseGroupRow).__group === true;
}
