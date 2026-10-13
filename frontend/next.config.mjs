/** @type {import('next').NextConfig} */
const dev = process.env.NODE_ENV !== "production";

// The dashboard loads nothing from other origins: scripts, styles, images, fonts and API calls are all its own.
// 'unsafe-inline' is what Next.js needs for its inline bootstrap scripts and for React's style attributes;
// 'unsafe-eval' is only for the development server (React Refresh). Framing and plugins are off entirely.
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  `connect-src 'self'${dev ? " ws: wss:" : ""}`,
  "frame-ancestors 'none'",
  "frame-src 'none'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
  // HSTS only when you really serve the dashboard over HTTPS (PJA_HTTPS=true); never for local http
  ...(process.env.PJA_HTTPS === "true"
    ? [{ key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" }]
    : []),
];

const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  productionBrowserSourceMaps: false,
  // allow opening the dev dashboard via 127.0.0.1 as well as localhost
  allowedDevOrigins: ["127.0.0.1"],
  agentRules: false,
  // /api/* is forwarded to the FastAPI backend by proxy.ts (which also guards it)
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
