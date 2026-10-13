// @vitest-environment node
/** proxy.ts - the dashboard server's guard in front of the API (host allow-list, cross-site check, API token). */
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

const TOKEN = "dashboard-test-token-" + "m4".repeat(12);

async function load(env: Record<string, string> = {}) {
  vi.resetModules();
  vi.unstubAllEnvs();
  for (const [k, v] of Object.entries(env)) vi.stubEnv(k, v);
  return import("@/proxy");
}

function request(url: string, init: { method?: string; headers?: Record<string, string> } = {}) {
  const u = new URL(url);
  return new NextRequest(url, { method: init.method ?? "GET", headers: { host: u.host, ...init.headers } });
}

afterEach(() => vi.unstubAllEnvs());

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
      expect(proxy(request("http://localhost:3000/review", { headers: { host } })).status).toBe(200);
    }
    for (const host of ["192.168.1.20:3000", "evil.example.com", "localhost.evil.example:3000", "my-mac.local:3000"]) {
      const page = proxy(request("http://localhost:3000/review", { headers: { host } }));
      const data = proxy(request("http://localhost:3000/api/profile", { headers: { host } }));
      expect([page.status, data.status]).toEqual([400, 400]);
      expect(await data.json()).toEqual({ detail: "Invalid Host header" });
    }
  });

  it("accepts an extra name only when you configure it", async () => {
    const { proxy } = await load({ PJA_DASHBOARD_HOSTS: "my-mac.local" });
    expect(proxy(request("http://localhost:3000/", { headers: { host: "my-mac.local:3000" } })).status).toBe(200);
    expect(proxy(request("http://localhost:3000/", { headers: { host: "other.local:3000" } })).status).toBe(400);
  });
});

describe("/api forwarding", () => {
  it("forwards same-origin calls to the backend, path and query intact", async () => {
    const { proxy } = await load({ PJA_BACKEND_URL: "http://127.0.0.1:8123/" });
    const res = proxy(request("http://localhost:3000/api/jobs?status=APPLIED&page=2", {
      headers: { "sec-fetch-site": "same-origin" },
    }));
    expect(res.headers.get("x-middleware-rewrite")).toBe("http://127.0.0.1:8123/api/jobs?status=APPLIED&page=2");
    const post = proxy(request("http://localhost:3000/api/agent/stop", {
      method: "POST", headers: { origin: "http://localhost:3000", "sec-fetch-site": "same-origin" },
    }));
    expect(post.headers.get("x-middleware-rewrite")).toBe("http://127.0.0.1:8123/api/agent/stop");
  });

  it("forwards as the dashboard server: the checked Origin / Sec-Fetch-* are not passed on", async () => {
    // (so the dashboard works on any port or host name you allow, without teaching the API about it)
    const { proxy } = await load({ PJA_DASHBOARD_HOSTS: "my-mac.local" });
    const res = proxy(request("http://my-mac.local:4000/api/agent/stop", {
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
    const res = proxy(request("http://localhost:3000/jobs/12"));
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
        const res = proxy(request("http://localhost:3000/api/agent/stop", { method, headers }));
        expect(res.status, JSON.stringify(headers)).toBe(403);
        expect(res.headers.get("x-middleware-rewrite")).toBeNull();
      }
    }
  });

  it("adds the API token on the server and drops credentials a page tried to send", async () => {
    const { proxy } = await load({ PJA_API_TOKEN: TOKEN });
    const res = proxy(request("http://localhost:3000/api/profile", {
      headers: { authorization: "Bearer forged-by-a-page", "x-pja-token": "forged-too" },
    }));
    const forwarded = res.headers.get("x-middleware-request-authorization");
    expect(forwarded).toBe(`Bearer ${TOKEN}`);
    expect(res.headers.get("x-middleware-request-x-pja-token")).toBeNull();
    expect(res.headers.get("x-middleware-override-headers") ?? "").not.toContain("x-pja-token");
  });

  it("sends no credential at all when no token is configured", async () => {
    const { proxy } = await load();
    const res = proxy(request("http://localhost:3000/api/profile", { headers: { authorization: "Bearer forged" } }));
    expect(res.headers.get("x-middleware-request-authorization")).toBeNull();
    expect(res.headers.get("x-middleware-override-headers") ?? "").not.toContain("authorization");
  });
});
