/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  // next build 不跑 ESLint: 历史代码有 react/no-unescaped-entities 等大量未清理的
  // 违规, 跑 lint 会挡构建。CI 里 `next lint` 仍会跑 (非阻塞), 清完 backlog 后再
  // 把这里去掉恢复 build 时检查。
  eslint: {
    ignoreDuringBuilds: true,
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_URL || "http://backend:8000"}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;

