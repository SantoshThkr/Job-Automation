// Runs on the dashboard server before every request (Next.js "proxy", formerly middleware).
//
// The dashboard is the only door to the API, and the API holds your profile and controls the browser agent, so:
//   1. Host allow-list - a request addressed to any other name is refused. This stops DNS rebinding (a web page
//      pointing its own domain at 127.0.0.1) and anyone reaching the dashboard under a LAN address.
//   2. Cross-site check - /api calls that another web site triggers from your browser (CSRF) are refused.
//   3. API token - when PJA_API_TOKEN is set it is added here, on the server. It is never sent to the browser,
//      never part of the client bundle (no NEXT_PUBLIC_ variable is used anywhere in this project).
//   4. /api/* is forwarded to the FastAPI backend (PJA_BACKEND_URL, default http://127.0.0.1:8010) - but only
//      after that address has answered as the Job Assistant API. (The API moved off the common port 8000, where
//      another project's container answered whenever this API was down.) Whatever answers there that is not this
//      API receives nothing - not a request, not the token.
import { NextResponse, type NextRequest } from "next/server";

const BACKEND = (process.env.PJA_BACKEND_URL || "http://127.0.0.1:8010").replace(/\/+$/, "");
const TOKEN = (process.env.PJA_API_TOKEN || "").trim();
// extra names the dashboard may be opened under (comma separated), e.g. a hostname you gave this machine
const EXTRA_HOSTS = (process.env.PJA_DASHBOARD_HOSTS || "").split(",").map((h) => h.trim().toLowerCase()).filter(Boolean);
const HOSTS = new Set(["localhost", "127.0.0.1", "::1", ...EXTRA_HOSTS]);

/** "localhost:3000" -> "localhost", "[::1]:3000" -> "::1" ("" when malformed). */
export function hostName(header: string | null): string {
  const h = (header || "").trim().toLowerCase();
  if (!h || /[\s/\\@,]/.test(h)) return "";
  if (h.startsWith("[")) {
    const end = h.indexOf("]");
    return end > 0 ? h.slice(1, end) : "";
  }
  const colons = h.split(":").length - 1;
  if (colons > 1) return h; // a bare IPv6 address
  return colons === 1 ? h.slice(0, h.lastIndexOf(":")) : h;
}

function refuse(status: number, detail: string): NextResponse {
  return NextResponse.json({ detail }, { status, headers: { "Cache-Control": "no-store" } });
}

export const SERVICE_NAME = "personal-job-assistant"; // what the API's /api/health answers as "service"
const IDENTITY_TTL_MS = 10_000; // a confirmed backend is trusted this long before it is asked again
const IDENTITY_RETRY_MS = 2_000; // a missing / foreign one is asked again this soon (the API may just be starting)
let identity: { ok: boolean; detail: string; at: number } | null = null;

/** Is the program answering on PJA_BACKEND_URL the Job Assistant API? (asked without the token; cached briefly) */
export async function backendIdentity(now: number = Date.now()): Promise<{ ok: boolean; detail: string }> {
  if (identity && now - identity.at < (identity.ok ? IDENTITY_TTL_MS : IDENTITY_RETRY_MS)) return identity;
  let ok = false;
  let detail = "";
  try {
    const res = await fetch(`${BACKEND}/api/health`, {
      headers: { accept: "application/json" }, cache: "no-store", signal: AbortSignal.timeout(2500),
    });
    const body: unknown = await res.json().catch(() => null);
    ok = res.ok && !!body && typeof body === "object" && (body as { service?: unknown }).service === SERVICE_NAME;
    if (!ok) {
      detail = `The program answering on ${BACKEND} is not the Job Assistant API - another service is using that `
        + "port. Start the API (make dev), or give it a port of its own: set PJA_API_PORT in .env and restart.";
    }
  } catch {
    detail = `The Job Assistant API is not reachable on ${BACKEND}. Start it with: make dev`;
  }
  identity = { ok, detail, at: now };
  return identity;
}

export async function proxy(request: NextRequest) {
  const hostHeader = request.headers.get("host");
  if (!HOSTS.has(hostName(hostHeader))) return refuse(400, "Invalid Host header");

  const { pathname, search } = request.nextUrl;
  if (pathname !== "/api" && !pathname.startsWith("/api/")) return NextResponse.next();

  // the dashboard's own pages call /api same-origin; anything else did not come from the dashboard
  const origin = (request.headers.get("origin") || "").toLowerCase().replace(/\/+$/, "");
  const own = [`http://${(hostHeader || "").toLowerCase()}`, `https://${(hostHeader || "").toLowerCase()}`];
  if (request.headers.get("sec-fetch-site") === "cross-site" || (origin && !own.includes(origin))) {
    return refuse(403, "Cross-site requests are not allowed");
  }

  const backend = await backendIdentity();
  if (!backend.ok) return refuse(503, backend.detail); // nothing - and no token - goes to an unknown program

  const headers = new Headers(request.headers);
  headers.delete("x-pja-token"); // whatever a page sent is not a credential
  headers.delete("authorization");
  if (TOKEN) headers.set("authorization", `Bearer ${TOKEN}`);
  // From here on this is the dashboard server calling the API, not a browser: the same-origin check was made
  // above, so the browser's Origin / Sec-Fetch-* are not passed on. (The API applies its own origin check to
  // whatever reaches it directly; forwarding them would tie it to the port the dashboard happens to run on.)
  for (const name of ["origin", "referer", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user"]) {
    headers.delete(name);
  }
  return NextResponse.rewrite(new URL(`${BACKEND}${pathname}${search}`), { request: { headers } });
}

export const config = {
  // every page and API call; static build assets carry no data and are skipped
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
