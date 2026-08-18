/** 携程预计收入(业主到手)展示格式化。后端只对 OTA 搬单单返回数值，否则 null/undefined。
 *  返回 null 时调用方不渲染该行/列。
 *  入参含 string：后端是 Decimal，Pydantic v2 在 JSON 里序列化成 "211.56" 而非数字——
 *  函数内部本就 Number(value)，这里只是把签名对齐运行时。 */
export function formatExpectedRevenue(
  value: number | string | null | undefined
): string | null {
  if (value == null) return null;
  return `¥${Number(value).toLocaleString()}`;
}
