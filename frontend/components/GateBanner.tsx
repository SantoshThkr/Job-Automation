"use client";

import Link from "next/link";
import { AgentState, api } from "@/lib/api";
import { useAction } from "@/lib/hooks";
import { timeAgo } from "@/lib/format";

const TITLES: Record<string, string> = {
  review: "Ready to submit - your approval needed",
  login: "Login needed",
  captcha: "CAPTCHA - please solve it in the browser",
  otp: "OTP / verification code needed",
  manual: "Your input is needed",
};

/** Shown whenever the agent is paused waiting for a human decision or action. */
export function GateBanner({ agent, onChange }: { agent: AgentState | null; onChange: () => void }) {
  const { busy, error, run } = useAction();
  if (!agent?.gate_kind) return null;
  const kind = agent.gate_kind;
  const isReview = kind === "review";
  const respond = (r: "approve" | "reject") => async () => {
    await run(() => api.gate(r, agent.pending_job_id));
    onChange();
  };
  return (
    <div className="alert warn" role="alert">
      <div className="title">{TITLES[kind] ?? "Waiting for you"} <span className="muted small">· {timeAgo(agent.gate_opened_at)}</span></div>
      <p>{agent.gate_reason}</p>
      {!isReview && (
        <p className="small muted">Do it in the browser window opened by the agent, then press Continue.</p>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <button className="btn ok" disabled={busy} onClick={respond("approve")}>
          {isReview ? "Approve & submit" : "Continue"}
        </button>
        <button className="btn danger" disabled={busy} onClick={respond("reject")}>
          {isReview ? "Reject" : "Skip"}
        </button>
        {agent.pending_job_id && <Link href={`/jobs/${agent.pending_job_id}`}>Open job details →</Link>}
      </div>
      {error && <p className="small lvl-error">{error}</p>}
    </div>
  );
}
