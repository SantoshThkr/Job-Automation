/**
 * The job-recency window: chosen in the dashboard, saved by the backend, shown on every job as its posting date.
 *
 * Background: the assistant applied to jobs posted months earlier (one on 2026-06-25, 111 days before) because
 * nothing checked when a job had been posted, and the dashboard showed only when it had been found.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JobSummary, Recency } from "@/lib/api";
import { localDay, postedAge, recencyOption } from "@/lib/format";
import { captureConsoleErrors, keyWarnings } from "./helpers";

const api = vi.hoisted(() => ({ setRecency: vi.fn() }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import { RecencyPicker, rangeProblem, recencyValue } from "@/components/RecencyPicker";
import { JobTable, PostedCell } from "@/components/JobTable";

const CHOICES = [1, 3, 7, 14, 30, 60, 90];
const recency = (over: Partial<Recency> = {}): Recency => ({
  days: 7, label: "posted in the last 7 days", chosen: false, choices: CHOICES, ...over,
});

const job = (over: Partial<JobSummary> = {}): JobSummary => ({
  id: 1, portal: "naukri", title: "Frontend Engineer", company: "Acme Labs", location: "Pune", url: "https://jobs.example.com/1",
  match_score: 88, decision: "APPLY", status: "QUEUED", status_reason: "", apply_type: "easy_apply",
  discovered_at: "2026-10-13T04:00:00Z", updated_at: "2026-10-13T04:00:00Z", resume_id: null, applied_at: null,
  ats_platform: null, frontend_ratio: 0.9, posted_at: "2026-10-12T04:00:00Z", posted_label: "on 2026-10-12 (2 days ago)",
  posted_age_days: 2.1, posted_open_ended: false, posted_source: "label", closed_reason: "", recency: "in",
  recency_reason: "", ...over,
});

let errors: string[];
let confirm: ReturnType<typeof vi.fn>;
beforeEach(() => {
  errors = captureConsoleErrors();
  api.setRecency.mockReset().mockResolvedValue({});
  confirm = vi.fn(() => true);
  vi.stubGlobal("confirm", confirm);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const select = () => screen.getByLabelText("Job recency") as HTMLSelectElement;

describe("choosing the job-recency window", () => {
  it("offers 24 hours, 3 to 90 days, a custom range and all dates - and shows the default as the last 7 days", () => {
    render(<RecencyPicker recency={recency()} onChange={() => {}} />);
    const names = Array.from(select().options).map((o) => o.textContent);
    expect(names).toEqual(["Last 24 hours", "Last 3 days", "Last 7 days", "Last 14 days", "Last 30 days", "Last 60 days",
      "Last 90 days", "Custom range…", "All dates"]);
    expect(select().value).toBe("7");
    expect(screen.getByTestId("recency-label").textContent).toContain("posted in the last 7 days (the default)");
    expect(keyWarnings(errors)).toEqual([]);
  });

  it("saves a choice in the backend (so it survives a refresh and every run) and reloads", async () => {
    const onChange = vi.fn();
    render(<RecencyPicker recency={recency()} onChange={onChange} />);
    fireEvent.change(select(), { target: { value: "30" } });
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    expect(api.setRecency).toHaveBeenCalledWith({ days: 30 });
  });

  it("shows the saved window after a reload, whatever was shown before", () => {
    const { rerender } = render(<RecencyPicker recency={recency()} onChange={() => {}} />);
    rerender(<RecencyPicker recency={recency({ days: 14, label: "posted in the last 14 days", chosen: true })} onChange={() => {}} />);
    expect(select().value).toBe("14");
    expect(screen.getByTestId("recency-label").textContent).not.toContain("default");
  });

  it("chooses 'All dates' only after you confirm it", async () => {
    confirm.mockReturnValue(false);
    render(<RecencyPicker recency={recency()} onChange={() => {}} />);
    fireEvent.change(select(), { target: { value: "all" } });
    expect(confirm).toHaveBeenCalled();
    expect(api.setRecency).not.toHaveBeenCalled();
    expect(select().value).toBe("7");

    confirm.mockReturnValue(true);
    fireEvent.change(select(), { target: { value: "all" } });
    await waitFor(() => expect(api.setRecency).toHaveBeenCalledWith({ all: true }));
  });

  it("says plainly when every posting date is allowed", () => {
    render(<RecencyPicker recency={recency({ days: undefined, all: true, label: "all dates", chosen: true })} onChange={() => {}} />);
    expect(select().value).toBe("all");
    expect(screen.getByTestId("recency-label").textContent).toContain("not limited");
  });

  it("saves a custom range only when it is valid", async () => {
    render(<RecencyPicker recency={recency()} onChange={() => {}} />);
    fireEvent.change(select(), { target: { value: "custom" } });
    expect(api.setRecency).not.toHaveBeenCalled(); // nothing is saved before the range is complete
    const apply = screen.getByRole("button", { name: "Apply range" }) as HTMLButtonElement;
    expect(apply.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Posted from"), { target: { value: localDay(-3) } });
    fireEvent.change(screen.getByLabelText("Posted until"), { target: { value: localDay(-5) } });
    expect(apply.disabled).toBe(true);
    expect(screen.getByText("The start date is after the end date.")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Posted until"), { target: { value: localDay(-1) } });
    expect(apply.disabled).toBe(false);
    fireEvent.click(apply);
    await waitFor(() => expect(api.setRecency).toHaveBeenCalledWith({ start: localDay(-3), end: localDay(-1) }));
  });

  it("shows a stored custom range with its dates", () => {
    render(<RecencyPicker recency={recency({ days: undefined, start: "2026-09-01", end: null, label: "posted 2026-09-01 to today", chosen: true })}
                          onChange={() => {}} />);
    expect(select().value).toBe("custom");
    expect((screen.getByLabelText("Posted from") as HTMLInputElement).value).toBe("2026-09-01");
  });

  it("shows the backend's refusal", async () => {
    api.setRecency.mockRejectedValue(new Error("days must be one of [1, 3, 7, 14, 30, 60, 90]"));
    render(<RecencyPicker recency={recency()} onChange={() => {}} />);
    fireEvent.change(select(), { target: { value: "3" } });
    expect(await screen.findByText("days must be one of [1, 3, 7, 14, 30, 60, 90]")).toBeTruthy();
  });

  it.each([
    ["", "", "Choose the first day of the range."],
    ["2026-10-10", "2026-10-01", "The start date is after the end date."],
    ["2026-10-10", "2026-10-20", "The range cannot end in the future."],
    ["2025-01-01", "2025-02-01", "The range must lie within the last year."],
    ["2025-10-13", "2025-10-20", ""], // 366 days back is still allowed, as in the backend
    ["2026-10-01", "", ""],
    ["2026-10-01", "2026-10-14", ""],
  ])("checks a range %s to %s like the backend does", (start, end, problem) => {
    expect(rangeProblem(start, end, "2026-10-14")).toBe(problem);
  });

  it("maps a stored window to its option", () => {
    expect(recencyValue(recency({ days: 1 }))).toBe("1");
    expect(recencyValue(recency({ days: undefined, all: true }))).toBe("all");
    expect(recencyValue(recency({ days: undefined, start: "2026-09-01" }))).toBe("custom");
    expect(recencyOption(1)).toBe("Last 24 hours");
    expect(recencyOption(60)).toBe("Last 60 days");
  });
});

describe("a job's posting date", () => {
  it("is shown with its age - never the day it was found", () => {
    render(<JobTable jobs={[job()]} />);
    const row = screen.getByText("Frontend Engineer").closest("tr")!;
    expect(within(row).getByTestId("posted-age").textContent).toBe("2 days ago");
    expect(within(row).queryByTestId("posted-flag")).toBeNull();
    expect(screen.getByText("Posted")).toBeTruthy();
  });

  it("says 'Unknown' when the portal did not show it", () => {
    render(<PostedCell job={job({ posted_at: null, posted_label: "", posted_age_days: null, recency: "unknown",
      recency_reason: "posting date unknown - not applied automatically" })} />);
    expect(screen.getByText("Unknown")).toBeTruthy();
  });

  it("flags a job outside the window, a closed one, and an open-ended label", () => {
    render(<>
      <PostedCell job={job({ id: 1, posted_age_days: 40, recency: "out", recency_reason: "outside your window" })} />
      <PostedCell job={job({ id: 2, closed_reason: "the listing's own end date (2026-09-23) has passed", recency: "out" })} />
      <PostedCell job={job({ id: 3, posted_age_days: 21.2, posted_open_ended: true, recency: "unknown" })} />
    </>);
    expect(screen.getAllByTestId("posted-flag").map((e) => e.textContent)).toEqual(["Too old", "Closed", "Unsure"]);
    expect(screen.getByText("21+ days ago")).toBeTruthy();
    expect(screen.getByText(/^on\/before /)).toBeTruthy();
  });

  it.each([
    [null, false, ""],
    [0.4, false, "today"],
    [1.2, false, "1 day ago"],
    [6.9, false, "6 days ago"],
    [21, true, "21+ days ago"],
  ])("ages %s days (open-ended %s) as '%s'", (days, open, text) => {
    expect(postedAge(days, open)).toBe(text);
  });
});
