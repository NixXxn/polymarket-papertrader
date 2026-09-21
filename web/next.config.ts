import type { NextConfig } from "next";

const FLASK_ORIGIN = process.env.PAPERTRADER_API_ORIGIN || "http://127.0.0.1:8787";

const nextConfig: NextConfig = {
  allowedDevOrigins: ["127.0.0.1"],
  turbopack: {
    root: process.cwd(),
  },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${FLASK_ORIGIN}/api/:path*` },
      { source: "/health", destination: `${FLASK_ORIGIN}/health` },
    ];
  },
};

export default nextConfig;
