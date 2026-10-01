function destinationByRole(role: string): string {
  if (role === "cleaner") return "/staff/cleaner";
  return "/staff/keeper";
}

export function safeStaffNext(next: string | null, role: string): string {
  const fallback = destinationByRole(role);
  if (!next || !next.startsWith("/") || next.startsWith("//")) return fallback;
  try {
    const rawPathname = next.split(/[?#]/, 1)[0];
    if (/\\|%(?:2f|5c|25(?:25)*(?:2f|5c))/i.test(rawPathname)) return fallback;
    const parsed = new URL(next, "https://staff.internal");
    const normalizedPathname = parsed.pathname.replace(/\/{2,}/g, "/").replace(/\/+$/, "") || "/";
    if (parsed.origin !== "https://staff.internal" || !normalizedPathname.startsWith("/staff/") || normalizedPathname === "/staff/login") return fallback;
    return `${normalizedPathname}${parsed.search}${parsed.hash}`;
  } catch {
    return fallback;
  }
}

