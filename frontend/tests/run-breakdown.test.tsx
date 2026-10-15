/**
 * A run, reconciled: sightings are not jobs, every decided job is counted once, and every skip says which rule.
 *
 * Background: the dashboard showed run 76 as "1832 discovered, 106 new, 77 skipped, 19 for a decision, 7 relevant,
 * 2 duplicates" - sightings next to job counts that did not add up, with no way to see why each job was skipped.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunBreakdown as Breakdown } from "@/lib/api";
import { captureConsoleErrors, keyWarnings } from "./helpers";

const api = vi.hoisted(() => ({ runBreakdown: vi.fn() }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("next/link", () => ({
  default: ({ href, children, title }: { href: string; children: React.ReactNode; title?: string }) => <a href={href} title={title}>{children}</a>,
}));

import { RunBreakdown } from "@/components/RunBreakdown";

const run76: Breakdown = {
  run: { id: 76, mode: "auto", status: "STOPPED", portals: ["instahyre", "linkedin", "naukri"] },
  funnel: { sightings: 1832, unique_listings: null, repeat_sightings: null, known_sightings: 1726, new_records: 106,
            read_again: 0, already_applied_on_portal: 0, searches_skipped_redundant: 0 },
  discovery: {
    jobs: 106,
    by_outcome: { relevant: 7, review: 19, skipped: 32, skipped_title: 45, too_old: 1, duplicate: 2 },
    by_portal: { instahyre: { skipped_title: 1 }, linkedin: { relevant: 7, review: 19, skipped: 32, skipped_title: 44, too_old: 1, duplicate: 2 } },
    by_stage: { title: 45, read: 58, "posting date": 1, duplicates: 2 },
    decided_by: { rules: 58 },
    reasons: [
      { outcome: "skipped_title", code: "TITLE_EXCLUDED_KEYWORD", meaning: "the title contains a keyword you excluded", count: 28,
        examples: [{ id: 2986, title: "React Native Developer", company: "Parallel Dots", portal: "instahyre", why: "Title contains excluded keyword 'react native'" }] },
      { outcome: "review", code: "FULLSTACK_BACKEND_HEAVY", meaning: "a full-stack role whose backend outweighs the frontend", count: 5,
        examples: [{ id: 3085, title: "Senior Full Stack Engineer - Python & ReactJS", company: "Checkmate", portal: "linkedin", why: "…" }] },
    ],
  },
  queue: { set_aside_by_date: 237 },
  reconciles: true,
  reconciliation: "1832 sightings = 1726 already known + 106 decided in this run (7 relevant, 19 review, 32 skipped, 45 skipped title, 1 too old, 2 duplicate)",
};

let errors: string[];
beforeEach(() => {
  errors = captureConsoleErrors();
  api.runBreakdown.mockReset().mockImplementation(async (_id: number, audit?: boolean) => audit
    ? { ...run76, audit: { skips_checked: 77, still_skipped: 67, would_apply: 0, would_put_to_you: 0, would_read_first: 10,
                           examples: [{ id: 3031, title: "Senior Forward Deployed AI Engineer", company: "Elife", now: "read_now" }] } }
    : run76);
});
afterEach(cleanup);

describe("a run's breakdown", () => {
  it("tells sightings from jobs and says that the numbers reconcile", async () => {
    render(<RunBreakdown runId={76} />);
    expect((await screen.findByTestId("reconciliation")).textContent).toContain("Reconciles: 1832 sightings = 1726 already known + 106 decided");
    expect(screen.getByText(/listing sightings \(a job listed by two searches counts twice\)/)).toBeTruthy();
    expect(screen.getByText("106 job(s) decided in this run")).toBeTruthy();
    expect(keyWarnings(errors)).toEqual([]);
  });

  it("counts each outcome once, by portal, and shows the old queued jobs the run set aside", async () => {
    render(<RunBreakdown runId={76} />);
    const row = (await screen.findByText("Skipped from the title")).closest("tr")!;
    expect(within(row).getAllByRole("cell").map((c) => c.textContent)).toEqual(["Skipped from the title", "45", "1", "44"]);
    expect(screen.getByText(/237 Queued earlier, now too old/)).toBeTruthy();
    expect(screen.getByText(/58 rules/)).toBeTruthy();
  });

  it("names the rule behind each group, with jobs to open", async () => {
    render(<RunBreakdown runId={76} />);
    const reasons = await screen.findByTestId("run-reasons");
    expect(within(reasons).getByText("FULLSTACK_BACKEND_HEAVY")).toBeTruthy();
    expect((within(reasons).getByText("Senior Full Stack Engineer - Python & ReactJS") as HTMLAnchorElement).getAttribute("href")).toBe("/jobs/3085");
  });

  it("audits the run's skips with today's rules only when asked", async () => {
    render(<RunBreakdown runId={76} />);
    fireEvent.click(await screen.findByRole("button", { name: /Audit the skips/ }));
    await waitFor(() => expect(api.runBreakdown).toHaveBeenLastCalledWith(76, true));
    expect((await screen.findByTestId("run-audit")).textContent).toContain("10 read before deciding");
  });
});
