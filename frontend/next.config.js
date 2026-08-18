/** @type {import('next').NextConfig} */
const nextConfig = {
  // Docker copies the standalone bundle, while Vercel builds its own function
  // output and expects the regular Next.js tracing artifacts.
  ...(process.env.VERCEL ? {} : { output: "standalone" }),
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
