import { NextResponse, type NextRequest } from "next/server";

const ROOT_BRAND = "example.invalid";
const ADMIN_HOST = `admin.${ROOT_BRAND}`;
const TUOGUAN_HOST = `tuoguan.${ROOT_BRAND}`;

export function middleware(req: NextRequest) {
  const host = (req.headers.get("host") || "").toLowerCase().split(":")[0];
  const { pathname, search } = req.nextUrl;

  const isAdmin = host === ADMIN_HOST;
  const isRootBrand = host === ROOT_BRAND || host === `www.${ROOT_BRAND}`;
  const isTuoguan = host === TUOGUAN_HOST;

  // 本地/vercel.app 预览域：不做任何子域路由，保留原行为（方便开发调试）
  if (!isAdmin && !isRootBrand && !isTuoguan) return NextResponse.next();

  // Vercel 已将 apex 307 到 www，middleware 不再做 www↔apex 跳转（避免死循环）
  const C_SIDE_CANONICAL = `www.${ROOT_BRAND}`;

  // 托管招商子域：整站只服务 /tuoguan 落地页
  if (isTuoguan) {
    if (pathname === "/") {
      return NextResponse.rewrite(new URL("/tuoguan" + search, req.url));
    }
    if (pathname.startsWith("/tuoguan")) {
      return NextResponse.next();
    }
    // 其他路径（/booking /dashboard /login 等）一律回托管首页
    // 保留 query（UTM 等投放参数），路径写错也不丢归因
    return NextResponse.redirect(new URL("/" + search, `https://${TUOGUAN_HOST}`), 302);
  }

  // 管理子域：屏蔽 C 端路径（/booking、/owner、/staff），整站只服务 B 端
  // 根路径 / 不拦,让 app/page.tsx 自己 redirect 到 /dashboard
  if (isAdmin) {
    if (
      pathname.startsWith("/booking") ||
      pathname.startsWith("/owner") ||
      pathname.startsWith("/staff")
    ) {
      return NextResponse.redirect(
        new URL(pathname + search, `https://${C_SIDE_CANONICAL}`),
        302
      );
    }
    return NextResponse.next();
  }

  // 品牌根域：屏蔽 B 端内部页路径,但 /login 放行作为统一入口
  if (isRootBrand) {
    // /dashboard/* → 去管理子域
    if (pathname.startsWith("/dashboard")) {
      return NextResponse.redirect(
        new URL(pathname + search, `https://${ADMIN_HOST}`),
        302
      );
    }
    // /login 不再屏蔽 —— 它是所有角色的统一登录入口
    // 根路径 → /booking
    if (pathname === "/") {
      return NextResponse.redirect(new URL("/booking" + search, req.url), 302);
    }
  }

  return NextResponse.next();
}

// 跳过 Next.js 内部资源、静态文件、API（API 直接走 next.config.js 的 proxy 到 Railway）
export const config = {
  matcher: ["/((?!api/|_next/|favicon.ico|robots.txt|sitemap.xml|.*\\..*).*)"],
};
