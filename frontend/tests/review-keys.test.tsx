/**
 * Regression: "Encountered two children with the same key, `on`" on /review.
 *
 * Root cause: a question's choices are rendered as <option key={text}>, and the form scanner had stored the DOM
 * default value "on" as the label of radio buttons it could not read - twice. The cards themselves were always
 * keyed by a unique job_id.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { captureConsoleErrors, idleAgent, keyWarnings, needsYouItem, question } from "./helpers";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: ReactNode }) => <a href={href} {...rest}>{children}</a>,
}));

const api = vi.hoisted(() => ({
  agent: vi.fn(), jobs: vi.fn(), needsYou: vi.fn(), jobAction: vi.fn(), answerNeedsYou: vi.fn(), gate: vi.fn(),
  saveAnswer: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api }));

import ReviewPage from "@/app/review/page";
import { AnswerForm } from "@/components/AnswerForm";
import { GateBanner } from "@/components/GateBanner";
import { NeedsYouCard } from "@/components/NeedsYouCard";

let errors: string[];
beforeEach(() => {
  errors = captureConsoleErrors();
  api.agent.mockResolvedValue(idleAgent);
  api.jobs.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
});
afterEach(cleanup);

const optionTexts = (select: HTMLElement) => within(select).getAllByRole("option").map((o) => o.textContent);

describe("the test harness", () => {
  it("sees React's duplicate-key warning when one is really produced", () => {
    render(<ul>{["on", "on"].map((o) => <li key={o}>{o}</li>)}</ul>);
    expect(keyWarnings(errors).join("\n")).toMatch(/same key/);
  });
});

describe("a question's choices", () => {
  it("renders each choice once without a key warning when the API repeats labels ('on', 'on')", () => {
    const item = needsYouItem({
      questions: [
        question({ id: 1, options: ["on", "on"] }),
        question({ id: 2, question: "Notice period?", field_type: "select", options: ["Select", "Yes", "No", "Yes", " No ", ""] }),
      ],
    });
    render(<NeedsYouCard item={item} onChange={() => {}} />);
    const selects = screen.getAllByRole("combobox").filter((s) => within(s).queryByText("Choose…"));
    expect(optionTexts(selects[0])).toEqual(["Choose…", "on"]);
    expect(optionTexts(selects[1])).toEqual(["Choose…", "Select", "Yes", "No"]);
    expect(keyWarnings(errors)).toEqual([]);
  });

  it("is safe in the live gate banner and the saved-answer form too", () => {
    const agent = {
      ...idleAgent, alive: true, gate_kind: "questions", gate_reason: "1 question",
      gate_payload: { questions: [
        { id: "pja-1", question: "Hybrid in Bangalore?", type: "radio", options: ["on", "on"], required: true },
        { id: "pja-2", question: "Country?", type: "select", options: ["India", "──────", "──────", "Other", "Other", "Nepal", "Bhutan"], required: true },
      ] },
    };
    render(<GateBanner agent={agent as never} onChange={() => {}} />);
    render(<AnswerForm qa={{ id: 9, question: "Shift?", field_type: "select", options: ["Day", "Day", "Night"], required: true,
      answer: null, confidence: 0, source: "", needs_review: true, note: "" }} onSaved={() => {}} />);
    expect(screen.getAllByRole("radio")).toHaveLength(1);
    expect(keyWarnings(errors)).toEqual([]);
  });
});

describe("/review - one card per waiting job", () => {
  it("renders every legitimate record, including jobs that look alike, with no key warning", async () => {
    api.needsYou.mockResolvedValue({
      items: [
        needsYouItem({ job_id: 221, questions: [question({ id: 253, options: ["on", "on"] }), question({ id: 254, options: ["on", "on"] })] }),
        needsYouItem({ job_id: 229, questions: [question({ id: 313, options: ["on", "on"] })] }),
        needsYouItem({ job_id: 230 }),  // same company and title as 221 / 229: a different job all the same
      ],
      total: 3, parked: 0, by_kind: { questions: 3 },
    });
    render(<ReviewPage />);
    await waitFor(() => expect(document.querySelectorAll(".needs-you")).toHaveLength(3));
    expect(screen.getByText("3 application(s) waiting")).toBeTruthy();
    expect([...document.querySelectorAll(".needs-you h3 a")].map((a) => a.getAttribute("href")))
      .toEqual(["/jobs/221", "/jobs/229", "/jobs/230"]);
    expect(keyWarnings(errors)).toEqual([]);
    expect(errors).toEqual([]);
  });

  it("shows a job once and reports it when the API repeats a job_id", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    api.needsYou.mockResolvedValue({
      items: [needsYouItem({ job_id: 7 }), needsYouItem({ job_id: 8 }), needsYouItem({ job_id: 7 })],
      total: 3, parked: 0, by_kind: { questions: 3 },
    });
    render(<ReviewPage />);
    await waitFor(() => expect(document.querySelectorAll(".needs-you")).toHaveLength(2));
    expect(screen.getByText("2 application(s) waiting")).toBeTruthy();
    await waitFor(() => expect(warn).toHaveBeenCalledWith(expect.stringContaining("job_id 7")));
    expect(keyWarnings(errors)).toEqual([]);
  });

  it("keeps a card's identity (and what you typed) across a refresh that reorders the list", async () => {
    const first = { items: [needsYouItem({ job_id: 1, questions: [question({ id: 11, options: [] })] }),
                            needsYouItem({ job_id: 2, questions: [question({ id: 21, options: [] })] })],
                    total: 2, parked: 0, by_kind: { questions: 2 } };
    api.needsYou.mockResolvedValueOnce(first).mockResolvedValue({ ...first, items: [...first.items].reverse() });
    const { rerender } = render(<ReviewPage />);
    await waitFor(() => expect(document.querySelectorAll(".needs-you")).toHaveLength(2));
    const box = document.querySelector<HTMLInputElement>('.needs-you a[href="/jobs/1"]')!
      .closest(".needs-you")!.querySelector<HTMLInputElement>('input[type="text"]')!;
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(box, { target: { value: "my answer" } });
    await api.needsYou();  // consume the first response; the next poll returns the reversed order
    rerender(<ReviewPage />);
    await waitFor(() => expect([...document.querySelectorAll(".needs-you h3 a")].map((a) => a.getAttribute("href")))
      .toEqual(["/jobs/2", "/jobs/1"]), { timeout: 8000 });
    const again = document.querySelector('.needs-you a[href="/jobs/1"]')!.closest(".needs-you")!
      .querySelector<HTMLInputElement>('input[type="text"]')!;
    expect(again.value).toBe("my answer");
    expect(keyWarnings(errors)).toEqual([]);
  }, 15000);
});
