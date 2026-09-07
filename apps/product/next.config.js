/** @type {import('next').NextConfig} */
const path = require("node:path");
const { resolveFrontendPublicEnv } = require("../../scripts/port-config.cjs");
const frontendEnv = resolveFrontendPublicEnv({
  cwd: __dirname,
  rootDir: path.resolve(__dirname, "../.."),
  nextEnvPreload: true,
});

const nextConfig = {
  reactStrictMode: true,
  env: {
    BACKEND_PORT: frontendEnv.BACKEND_PORT,
    NEXT_PUBLIC_BACKEND_PORT: frontendEnv.NEXT_PUBLIC_BACKEND_PORT,
    REHAB_BACKEND_BASE: frontendEnv.REHAB_BACKEND_BASE,
    ...(frontendEnv.NEXT_PUBLIC_API_BASE ? { NEXT_PUBLIC_API_BASE: frontendEnv.NEXT_PUBLIC_API_BASE } : {}),
    ...(frontendEnv.NEXT_PUBLIC_WS_BASE ? { NEXT_PUBLIC_WS_BASE: frontendEnv.NEXT_PUBLIC_WS_BASE } : {}),
  },
  transpilePackages: ['@rehab/shared'],
  eslint: {
    ignoreDuringBuilds: true,
  },
  typescript: {
    ignoreBuildErrors: true,
  },
  experimental: {
    webpackBuildWorker: false,
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: frontendEnv.REHAB_BACKEND_BASE + "/:path*",
      },
    ];
  },
  outputFileTracingExcludes: {
    'next-server': ['**/*'],
    '*': ['**/*'],
  },
};

module.exports = nextConfig;
