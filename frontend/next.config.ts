import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // why: a stray package-lock.json outside this repo (in the machine's home
  // directory, from an unrelated global npm install) made Next.js infer the
  // wrong workspace root and warn about it on every build. Pinning it to this
  // directory is the fix Next's own warning points at, not something to
  // silence by deleting a lockfile this project doesn't own.
  outputFileTracingRoot: path.join(import.meta.dirname),
  // We lint with our own flat config (`pnpm lint`); Next's bundled
  // eslint-config-next is incompatible with ESLint 9 flat config.
  eslint: { ignoreDuringBuilds: true },
  // The FE is a thin renderer: no secrets, no server intelligence. Only the
  // public API base is exposed to the browser.
  env: {
    NEXT_PUBLIC_API_BASE: process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000",
  },
};

export default nextConfig;
