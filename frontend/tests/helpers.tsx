import { vi } from "vitest";
import type { AgentState, NeedsYouItem, NeedsYouQuestion } from "@/lib/api";

/** Everything React (or anything else) reports through console.error while a test runs. */
export function captureConsoleErrors(): string[] {
  const errors: string[] = [];
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    errors.push(args.map((a) => (a instanceof Error ? a.message : String(a))).join(" "));
  });
  return errors;
}

export const keyWarnings = (errors: string[]) =>
  errors.filter((e) => /same key|unique "key" prop|Encountered two children/i.test(e));

export function question(over: Partial<NeedsYouQuestion> = {}): NeedsYouQuestion {
  return {
    id: 1, question: "Are you comfortable working from the office?", field_type: "radio", options: ["Yes", "No"],
    required: true, why: "", category: "", profile_can_answer: false, missing: "Not in your profile",
    saved_answer: null, saved_scope: null, suggestion: null, suggestion_note: "", sensitive: false, ...over,
  };
}

export function needsYouItem(over: Partial<NeedsYouItem> = {}): NeedsYouItem {
  return {
    job_id: 101, company: "Acme Labs", title: "Frontend Engineer", portal: "linkedin",
    url: "https://jobs.example.com/view/101", ats: "", status: "MANUAL_ACTION_REQUIRED", step: "", problem: "Questions need your answers",
    stop_kind: "questions", why: "Required questions the assistant could not answer", live: false, resume: "restart",
    resume_note: "Retry starts this application again.", submit_attempted: false, verification: "", evidence: [],
    final_url: "", updated_at: "2026-10-12T04:00:00", questions: [question()], unanswered: 1, ...over,
  };
}

export const idleAgent = {
  desired_state: "STOPPED", actual_state: "STOPPED", alive: false, mode: "dry_run", portals: [], pid: null,
  heartbeat_at: null, started_at: null, run_id: null, current_activity: "", message: "", last_error: "",
  pending_job_id: null, gate_kind: null, gate_reason: "", gate_opened_at: null, gate_payload: {}, repeat_minutes: 0,
  phase: "", current_step: "", current_url: "", current_job: null,
} as unknown as AgentState;
