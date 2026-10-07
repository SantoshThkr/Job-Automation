/** @type {import('next').NextConfig} */
const backend = process.env.PJA_BACKEND_URL || "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  // allow opening the dev dashboard via 127.0.0.1 as well as localhost
  allowedDevOrigins: ["127.0.0.1"],
  agentRules: false,
  async rewrites() {
    // The dashboard talks to the FastAPI backend through this proxy (no CORS needed)
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
