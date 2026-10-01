function stableHash(value: string) {
  let high = 0xcbf29ce4;
  let low = 0x84222325;
  for (let index = 0; index < value.length; index += 1) {
    low = (low ^ value.charCodeAt(index)) >>> 0;
    const lowProduct = low * 0x1b3;
    high =
      (Math.imul(high, 0x1b3) +
        Math.floor(lowProduct / 0x100000000) +
        ((low << 8) >>> 0)) >>>
      0;
    low = lowProduct >>> 0;
  }
  return `${high.toString(16).padStart(8, "0")}${low
    .toString(16)
    .padStart(8, "0")}`;
}

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
        .map(([key, item]) => [key, canonicalValue(item)]),
    );
  }
  return value;
}

export function sourceRequestId(
  source: string,
  billingMonth: string,
  documentId: string,
  confirmedMapping: unknown,
) {
  const canonicalMapping = JSON.stringify(canonicalValue(confirmedMapping));
  return `${source}:${billingMonth}:${documentId}:${stableHash(canonicalMapping)}`;
}
