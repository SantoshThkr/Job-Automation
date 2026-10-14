// @vitest-environment node
/** proxy.ts - the dashboard server's guard in front of the API (host allow-list, cross-site check, API token). */
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

const TOKEN = "dashboard-test-token-" + "m4".repeat(12);

/** What answers on the backend address: by default the Job Assistant API itself. */
const backend = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>();
const asApi = () => Promise.resolve(Response.json({ ok: true, service: "personal-job-assistant" }));

async function load(env: Record<string, string> = {}) {
  vi.resetModules();
  vi.unstubAllEnvs();
  for (const [k, v] of Object.entries(env)) vi.stubEnv(k, v);
  backend.mockReset();
  backend.mockImplementation(asApi);
  vi.stubGlobal("fetch", backend);
  return import("@/proxy");
}

function request(url: string, init: { method?: string; headers?: Record<string, string> } = {}) {
  const u = new URL(url);
  return new NextRequest(url, { method: init.method ?? "GET", headers: { host: u.host, ...init.headers } });
}

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

describe("host allow-list", () => {
  it("parses host headers", async () => {
    const { hostName } = await load();
    expect(hostName("localhost:3000")).toBe("localhost");
    expect(hostName("[::1]:3000")).toBe("::1");
    expect(hostName("127.0.0.1")).toBe("127.0.0.1");
    for (const bad of ["", null, "a b", "user@host", "host/path", "[::1"]) expect(hostName(bad)).toBe("");
  });

  it("serves the dashboard on loopback names only", async () => {
    const { proxy } = await load();
    for (const host of ["localhost:3000", "127.0.0.1:3000", "[::1]:3000"]) {
      expect((await proxy(request("http://localhost:3000/review", { headers: { host } }))).status).toBe(200);
    }
    for (const host of ["192.168.1.20:3000", "evil.example.com", "localhost.evil.example:3000", "my-mac.local:3000"]) {
      const page = await proxy(request("http://localhost:3000/review", { headers: { host } }));
      const data = await proxy(request("http://localhost:3000/api/profile", { headers: { host } }));
      expect([page.status, data.status]).toEqual([400, 400]);
      expect(await data.json()).toEqual({ detail: "Invalid Host header" });
    }
  });

  it("accepts an extra name only when you configure it", async () => {
    const { proxy } = await load({ PJA_DASHBOARD_HOSTS: "my-mac.local" });
    expect((await proxy(request("http://localhost:3000/", { headers: { host: "my-mac.local:3000" } }))).status).toBe(200);
    expect((await proxy(request("http://localhost:3000/", { headers: { host: "other.local:3000" } }))).status).toBe(400);
  });
});

describe("/api forwarding", () => {
  it("forwards same-origin calls to the backend, path and query intact", async () => {
    const { proxy } = await load({ PJA_BACKEND_URL: "http://127.0.0.1:8123/" });
    const res = await proxy(request("http://localhost:3000/api/jobs?status=APPLIED&page=2", {
      headers: { "sec-fetch-site": "same-origin" },
    }));
    expect(res.headers.get("x-middleware-rewrite")).toBe("http://127.0.0.1:8123/api/jobs?status=APPLIED&page=2");
    const post = await proxy(request("http://localhost:3000/api/agent/stop", {
      method: "POST", headers: { origin: "http://localhost:3000", "sec-fetch-site": "same-origin" },
    }));
    expect(post.headers.get("x-middleware-rewrite")).toBe("http://127.0.0.1:8123/api/agent/stop");
  });

  it("forwards as the dashboard server: the checked Origin / Sec-Fetch-* are not passed on", async () => {
    // (so the dashboard works on any port or host name you allow, without teaching the API about it)
    const { proxy } = await load({ PJA_DASHBOARD_HOSTS: "my-mac.local" });
    const res = await proxy(request("http://my-mac.local:4000/api/agent/stop", {
      method: "POST",
      headers: { origin: "http://my-mac.local:4000", "sec-fetch-site": "same-origin", referer: "http://my-mac.local:4000/review",
                 "content-type": "application/json" },
    }));
    expect(res.status).toBe(200);
    const kept = (res.headers.get("x-middleware-override-headers") ?? "").split(",");
    expect(kept).toContain("content-type");
    for (const gone of ["origin", "referer", "sec-fetch-site"]) expect(kept).not.toContain(gone);
  });

  it("leaves pages alone", async () => {
    const { proxy } = await load();
    const res = await proxy(request("http://localhost:3000/jobs/12"));
    expect(res.headers.get("x-middleware-rewrite")).toBeNull();
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });

  it("refuses calls made by another web site", async () => {
    const { proxy } = await load();
    const cases: Record<string, string>[] = [
      { "sec-fetch-site": "cross-site" },
      { origin: "https://evil.example.com" },
      { origin: "null" },
      { origin: "http://localhost:3000.evil.example" },
      { origin: "http://127.0.0.1:3000" }, // a different origin than the one the page was opened under
    ];
    for (const headers of cases) {
      for (const method of ["GET", "POST", "DELETE"]) {
        const res = await proxy(request("http://localhost:3000/api/agent/stop", { method, headers }));
        expect(res.status, JSON.stringify(headers)).toBe(403);
        expect(res.headers.get("x-middleware-rewrite")).toBeNull();
      }
    }
  });

  it("adds the API token on the server and drops credentials a page tried to send", async () => {
    const { proxy } = await load({ PJA_API_TOKEN: TOKEN });
    const res = await proxy(request("http://localhost:3000/api/profile", {
      headers: { authorization: "Bearer forged-by-a-page", "x-pja-token": "forged-too" },
    }));
    const forwarded = res.headers.get("x-middleware-request-authorization");
    expect(forwarded).toBe(`Bearer ${TOKEN}`);
    expect(res.headers.get("x-middleware-request-x-pja-token")).toBeNull();
    expect(res.headers.get("x-middleware-override-headers") ?? "").not.toContain("x-pja-token");
  });

  it("sends no credential at all when no token is configured", async () => {
    const { proxy } = await load();
    const res = await proxy(request("http://localhost:3000/api/profile", { headers: { authorization: "Bearer forged" } }));
    expect(res.headers.get("x-middleware-request-authorization")).toBeNull();
    expect(res.headers.get("x-middleware-override-headers") ?? "").not.toContain("authorization");
  });
});

describe("the program on the API port", () => {
  // 2026-10-13: another project's container published port 8000 on every interface. Whenever this API was down,
  // the dashboard's requests - and its token - went to that container, which answers /api/health with 200 as well.
  const call = (proxy: (r: NextRequest) => Promise<Response>, path = "/api/jobs") =>
    proxy(request(`http://localhost:3000${path}`, { headers: { "sec-fetch-site": "same-origin" } }));

  it("is asked who it is - without the token - before anything is forwarded", async () => {
    const { proxy } = await load({ PJA_API_TOKEN: TOKEN, PJA_BACKEND_URL: "http://127.0.0.1:8123" });
    const res = await call(proxy);
    expect(res.headers.get("x-middleware-rewrite")).toBe("http://127.0.0.1:8123/api/jobs");
    expect(backend).toHaveBeenCalledTimes(1);
    const [url, init] = backend.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8123/api/health");
    expect(JSON.stringify(init?.headers ?? {})).not.toContain(TOKEN);
    expect(JSON.stringify(init?.headers ?? {}).toLowerCase()).not.toContain("authorization");
  });

  it.each([
    ["another service that also says it is healthy", () => Promise.resolve(Response.json({ ok: true }))],
    ["another service with its own health format", () => Promise.resolve(Response.json({ status: "healthy", service: "other-api" }))],
    ["a web page", () => Promise.resolve(new Response("<html>hello</html>", { headers: { "content-type": "text/html" } }))],
    ["an error from something else", () => Promise.resolve(Response.json({ detail: "Not Found" }, { status: 404 }))],
  ])("refuses to forward to %s", async (_what, answer) => {
    const { proxy } = await load({ PJA_API_TOKEN: TOKEN });
    backend.mockImplementation(answer);
    for (const method of ["GET", "POST"]) {
      const res = await proxy(request("http://localhost:3000/api/agent/start", {
        method, headers: { origin: "http://localhost:3000", "sec-fetch-site": "same-origin" },
      }));
      expect(res.status).toBe(503);
      expect(res.headers.get("x-middleware-rewrite")).toBeNull(); // nothing is forwarded...
      expect(res.headers.get("x-middleware-request-authorization")).toBeNull(); // ...and the token stays here
      const { detail } = await res.json();
      expect(detail).toContain("is not the Job Assistant API");
      expect(detail).toContain("PJA_API_PORT");
      expect(detail).not.toContain(TOKEN);
    }
  });

  it("says so when nothing answers at all", async () => {
    const { proxy } = await load();
    backend.mockImplementation(() => Promise.reject(new TypeError("fetch failed")));
    const res = await call(proxy);
    expect(res.status).toBe(503);
    expect((await res.json()).detail).toContain("not reachable");
  });

  it("still serves the dashboard's own pages when the API is away", async () => {
    const { proxy } = await load();
    backend.mockImplementation(() => Promise.reject(new TypeError("fetch failed")));
    const page = await proxy(request("http://localhost:3000/review"));
    expect(page.headers.get("x-middleware-next")).toBe("1");
    expect(backend).not.toHaveBeenCalled(); // pages need no backend: it is not even asked
  });

  it("asks once for a burst of calls, again after a while, and soon again after a refusal", async () => {
    const { backendIdentity } = await load();
    const t0 = 1_000_000;
    expect((await backendIdentity(t0)).ok).toBe(true);
    await backendIdentity(t0 + 500);
    await backendIdentity(t0 + 9_000);
    expect(backend).toHaveBeenCalledTimes(1); // trusted for ten seconds
    await backendIdentity(t0 + 10_500);
    expect(backend).toHaveBeenCalledTimes(2);
    backend.mockImplementation(() => Promise.resolve(Response.json({ ok: true }))); // the API went away; something else answers
    expect((await backendIdentity(t0 + 21_000)).ok).toBe(false);
    await backendIdentity(t0 + 22_000);
    expect(backend).toHaveBeenCalledTimes(3); // (not hammered either)
    backend.mockImplementation(asApi); // the API is back
    expect((await backendIdentity(t0 + 23_500)).ok).toBe(true);
  });
});
