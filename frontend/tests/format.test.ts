import { describe, expect, it } from "vitest";
import { safeHref, uniqueById, uniqueStrings } from "@/lib/format";

describe("uniqueStrings", () => {
  it("keeps the first of each text, trimmed, in order", () => {
    expect(uniqueStrings(["on", "on"])).toEqual(["on"]);
    expect(uniqueStrings([" Yes", "No", "Yes ", "", null, undefined, "no"])).toEqual(["Yes", "No", "no"]);
    expect(uniqueStrings(undefined)).toEqual([]);
  });
});

describe("uniqueById", () => {
  it("keeps records with distinct ids and names the repeated ones", () => {
    const a = { job_id: 1, n: "a" }, b = { job_id: 2, n: "b" }, a2 = { job_id: 1, n: "a again" };
    expect(uniqueById([a, b], (r) => r.job_id)).toEqual({ items: [a, b], duplicates: [] });
    expect(uniqueById([a, b, a2, a2], (r) => r.job_id)).toEqual({ items: [a, b], duplicates: [1] });
  });
});

describe("safeHref", () => {
  it("passes web addresses and dashboard paths", () => {
    expect(safeHref("https://jobs.example.com/view/1?x=1")).toBe("https://jobs.example.com/view/1?x=1");
    expect(safeHref("http://careers.example.org/apply")).toBe("http://careers.example.org/apply");
    expect(safeHref("/api/screenshots/a.png")).toBe("/api/screenshots/a.png");
  });
  it("refuses everything that is not a web address", () => {
    for (const bad of ["javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,<script>alert(1)</script>",
                       "file:///etc/passwd", "vbscript:x", "//evil.example/x", "not a url", "", null, undefined]) {
      expect(safeHref(bad)).toBeUndefined();
    }
  });
});
