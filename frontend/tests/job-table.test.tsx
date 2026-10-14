/**
 * A job's status shows why - in the row, not only in a tooltip.
 *
 * Background: a "Senior Frontend Engineer" with a match of 85 was listed as "Skipped" with nothing next to it. The
 * reason ("Requires 10+ years; you have 7 ...") existed, but only as the cell's hover text.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobSummary } from "@/lib/api";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import { JobTable } from "@/components/JobTable";
import { NeedsYouCard } from "@/components/NeedsYouCard";
import { needsYouItem, question } from "./helpers";

vi.mock("next/link", () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a>,
}));
vi.mock("@/lib/api", () => ({ api: { jobAction: vi.fn(), answerNeedsYou: vi.fn() } }));

afterEach(cleanup);

const job = (over: Partial<JobSummary>): JobSummary => ({
  id: 1, portal: "linkedin", title: "Senior Frontend Engineer", company: "Acme Labs", location: "Hyderabad",
  url: "https://jobs.example.com/1", match_score: 84.6, decision: "SKIP", status: "SKIPPED", status_reason: "",
  apply_type: "easy_apply", discovered_at: "2026-10-13T04:00:00", updated_at: "2026-10-13T04:00:00", resume_id: null,
  applied_at: null, ats_platform: null, frontend_ratio: 0.9, ...over,
} as JobSummary);

describe("the job table", () => {
  it("shows the reason of a skipped job next to its high score", () => {
    const why = "Requires 10+ years; you have 7 (a hard requirement - skipped although the overall fit is 84.6)";
    render(<JobTable jobs={[job({ status_reason: why })]} />);
    const row = screen.getByText("Senior Frontend Engineer").closest("tr")!;
    expect(within(row).getByText("85")).toBeTruthy();
    expect(within(row).getByText("Skipped")).toBeTruthy();
    expect(within(row).getByTestId("status-reason").textContent).toBe(why);
  });

  it("explains every status that means 'not applied to', and leaves the others clean", () => {
    render(<JobTable jobs={[
      job({ id: 1, title: "A", status: "REVIEW_PENDING", status_reason: "Full-stack role is backend-heavy (frontend ≈ 44%)" }),
      job({ id: 2, title: "B", status: "MANUAL_ACTION_REQUIRED", status_reason: "Questions need your answers" }),
      job({ id: 3, title: "C", status: "FAILED", status_reason: "The company's site did not load" }),
      job({ id: 4, title: "D", status: "APPLIED", status_reason: "Applied with LinkedIn Easy Apply" }),
      job({ id: 5, title: "E", status: "QUEUED", status_reason: "Relevant (score 91)" }),
      job({ id: 6, title: "F", status: "SKIPPED", status_reason: "" }),
    ]} />);
    const reasons = screen.getAllByTestId("status-reason").map((e) => e.textContent);
    expect(reasons).toEqual(["Full-stack role is backend-heavy (frontend ≈ 44%)", "Questions need your answers",
      "The company's site did not load"]);
    expect(screen.getByText("Needs decision")).toBeTruthy(); // REVIEW_PENDING: whether to apply is your call
  });
});

describe("a job that waits for your decision", () => {
  it("is labelled 'Needs decision', not 'Questions'", () => {
    render(<NeedsYouCard onChange={() => undefined} item={needsYouItem({
      stop_kind: "decision", why: "A personal preference or a consequential answer that only you can decide",
      questions: [question({ question: "Are you willing to take a drug test?", decision: true })],
    })} />);
    expect(screen.getByText("Needs decision")).toBeTruthy();
    expect(screen.queryByText("Questions")).toBeNull();
  });
});
