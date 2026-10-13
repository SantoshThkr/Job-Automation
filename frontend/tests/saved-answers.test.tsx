/**
 * Saved answers: every answer says where it is reused, and can be corrected (not only deleted).
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryItem } from "@/lib/api";
import { captureConsoleErrors, keyWarnings } from "./helpers";

const api = vi.hoisted(() => ({ updateMemory: vi.fn(), deleteMemory: vi.fn() }));
vi.mock("@/lib/api", () => ({ api }));

import { SavedAnswers, scopeLabel } from "@/components/SavedAnswers";

const item = (over: Partial<MemoryItem> = {}): MemoryItem => ({
  id: 1, question: "What is your date of birth?", answer: "1 January 1990", times_used: 2,
  updated_at: "2026-10-12T04:00:00", scope: "global", company: "", portal: "", job: "", category: "SENSITIVE_ID", ...over,
});

let errors: string[];
beforeEach(() => {
  errors = captureConsoleErrors();
  api.updateMemory.mockReset().mockResolvedValue(item());
  api.deleteMemory.mockReset().mockResolvedValue({ ok: true });
});
afterEach(cleanup);

describe("where a saved answer is reused", () => {
  it.each([
    [item(), "everywhere"],
    [item({ scope: "portal", portal: "naukri" }), "only on Naukri"],
    [item({ scope: "company", company: "acme labs" }), "only for acme labs"],
    [item({ scope: "job", company: "acme labs", job: "frontend engineer" }), 'only for the job "frontend engineer" at acme labs'],
    [item({ scope: "category" }), "every wording of this question"],
  ])("is said in words", (m, label) => {
    expect(scopeLabel(m)).toBe(label);
  });

  it("the same question saved for several scopes is listed once per scope, without key warnings", () => {
    render(<SavedAnswers onChange={() => {}} items={[
      item({ id: 1 }), item({ id: 2, scope: "company", company: "acme labs", answer: "2 February 1990" }),
      item({ id: 3, scope: "job", company: "acme labs", job: "frontend engineer", answer: "3 March 1990" }),
    ]} />);
    expect(screen.getAllByText("What is your date of birth?")).toHaveLength(3);
    expect(screen.getByText(/reused only for acme labs/)).toBeTruthy();
    expect(keyWarnings(errors)).toEqual([]);
  });
});

describe("correcting a saved answer", () => {
  it("sends the corrected text for that answer and refreshes the list", async () => {
    const onChange = vi.fn();
    render(<SavedAnswers items={[item({ id: 7 })]} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByLabelText("Corrected answer for: What is your date of birth?");
    fireEvent.change(box, { target: { value: "  5 May 1991  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.updateMemory).toHaveBeenCalledWith(7, "5 May 1991"));
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
  });

  it("cannot save an empty or unchanged answer, and Cancel keeps the old one", () => {
    render(<SavedAnswers items={[item()]} onChange={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true); // unchanged
    fireEvent.change(screen.getByLabelText(/Corrected answer/), { target: { value: "   " } });
    expect(save.disabled).toBe(true); // empty
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByText(/1 January 1990/)).toBeTruthy();
    expect(api.updateMemory).not.toHaveBeenCalled();
  });

  it("shows the server's refusal instead of pretending it was saved", async () => {
    api.updateMemory.mockRejectedValue(new Error("answer is required"));
    const onChange = vi.fn();
    render(<SavedAnswers items={[item()]} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText(/Corrected answer/), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText(/answer is required/)).toBeTruthy());
    expect(onChange).not.toHaveBeenCalled();
  });
});
