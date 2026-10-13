"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { AgentState, GateQuestion, api } from "@/lib/api";
import { useAction } from "@/lib/hooks";
import { hostOf, timeAgo, uniqueStrings } from "@/lib/format";

const TITLES: Record<string, string> = {
  review: "Ready to submit - your approval needed",
  login: "Login needed",
  account: "Sign-in needed on the company site",
  captcha: "CAPTCHA - please solve it in the browser",
  otp: "OTP / verification code needed",
  verify: "Email verification needed",
  questions: "The application has questions for you",
  manual: "Your input is needed",
};

const BROWSER_HINT: Record<string, string> = {
  login: "Log in in the agent's browser window, then press Continue.",
  account: "Sign in or create the account in the agent's browser window, then press Continue. It continues from that page.",
  captcha: "Solve it in the agent's browser window (do not submit), then press Continue.",
  otp: "Enter the code in the agent's browser window, then press Continue.",
  verify: "Open the verification email, complete it, then press Continue.",
  manual: "Do it in the agent's browser window, then press Continue. It continues from where it stopped.",
};

function QuestionInput({ q, value, onChange }: { q: GateQuestion; value: string; onChange: (v: string) => void }) {
  const options = uniqueStrings(q.options);
  if (options.length && options.length <= 4 && ["radio", "checkbox_group", "select"].includes(q.type)) {
    return (
      <div className="row">
        {options.map((o) => (
          <label key={o} className="check small">
            <input type="radio" name={q.id} checked={value === o} onChange={() => onChange(o)} /> {o}
          </label>
        ))}
      </div>
    );
  }
  if (options.length) {
    return (
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={q.question}>
        <option value="">Choose…</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  }
  if (q.type === "textarea") {
    return <textarea rows={3} value={value} onChange={(e) => onChange(e.target.value)} aria-label={q.question} />;
  }
  return <input value={value} onChange={(e) => onChange(e.target.value)} aria-label={q.question}
                type={q.type === "number" ? "number" : "text"} />;
}

/** Shown whenever the agent is paused waiting for a human decision, answer or action. */
export function GateBanner({ agent, onChange }: { agent: AgentState | null; onChange: () => void }) {
  const { busy, error, run } = useAction();
  const questions = agent?.gate_kind === "questions" ? agent.gate_payload?.questions ?? [] : [];
  const gateKey = `${agent?.gate_kind}|${agent?.gate_opened_at}`;
  const [answers, setAnswers] = useState<Record<string, string>>({});

  useEffect(() => {
    const init: Record<string, string> = {};
    questions.forEach((q) => { if (q.suggested) init[q.id] = q.suggested; });
    setAnswers(init);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gateKey]);

  if (!agent?.gate_kind) return null;
  const kind = agent.gate_kind;
  const isReview = kind === "review";
  const respond = (r: "approve" | "reject", withAnswers = false) => async () => {
    await run(() => api.gate(r, agent.pending_job_id, withAnswers ? answers : undefined));
    onChange();
  };
  const missing = questions.filter((q) => q.required && !(answers[q.id] ?? "").trim());

  return (
    <div className="alert warn" role="alert">
      <div className="title">
        {TITLES[kind] ?? "Waiting for you"} <span className="muted small">· {timeAgo(agent.gate_opened_at)}</span>
      </div>
      {agent.current_job && (
        <p className="small">
          <Link href={`/jobs/${agent.current_job.id}`}>{agent.current_job.title} @ {agent.current_job.company}</Link>
          {agent.current_url && <span className="muted"> · on {hostOf(agent.current_url)}</span>}
        </p>
      )}
      {questions.length > 0 ? (
        <>
          <p className="small">Your answers are filled in, saved, and reused automatically on future applications.</p>
          <div className="questions">
            {questions.map((q) => (
              <div key={q.id} className="question">
                <label className="q">{q.question}{q.required && <span className="lvl-error"> *</span>}</label>
                <QuestionInput q={q} value={answers[q.id] ?? ""}
                               onChange={(v) => setAnswers((cur) => ({ ...cur, [q.id]: v }))} />
                {q.suggested && <div className="muted small">Suggested (low confidence): {q.suggested}</div>}
              </div>
            ))}
          </div>
          <div className="row" style={{ marginTop: 10 }}>
            <button className="btn ok" disabled={busy || missing.length > 0} onClick={respond("approve", true)}>
              Save answers &amp; continue
            </button>
            <button className="btn" disabled={busy} onClick={respond("approve")}
                    title="You typed the answers directly in the browser window">
              I filled them in the browser
            </button>
            <button className="btn danger" disabled={busy} onClick={respond("reject")}>Skip this job</button>
          </div>
        </>
      ) : (
        <>
          <p>{agent.gate_reason}</p>
          {!isReview && <p className="small muted">{BROWSER_HINT[kind] ?? BROWSER_HINT.manual}</p>}
          <div className="row" style={{ marginTop: 8 }}>
            <button className="btn ok" disabled={busy} onClick={respond("approve")}>
              {isReview ? "Approve & submit" : "Continue"}
            </button>
            <button className="btn danger" disabled={busy} onClick={respond("reject")}>
              {isReview ? "Reject" : "Skip this job"}
            </button>
            {agent.pending_job_id && <Link href={`/jobs/${agent.pending_job_id}`}>Open job details →</Link>}
          </div>
        </>
      )}
      {error && <p className="small lvl-error">{error}</p>}
    </div>
  );
}
