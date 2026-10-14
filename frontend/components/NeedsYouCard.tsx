"use client";

import Link from "next/link";
import { useState } from "react";
import { AnswerScope, NeedsYouItem, NeedsYouQuestion, api } from "@/lib/api";
import { useAction } from "@/lib/hooks";
import { PORTAL_LABEL, fmtDate, hostOf, safeHref, uniqueStrings } from "@/lib/format";

const KIND_LABEL: Record<string, string> = {
  questions: "Questions", decision: "Needs decision", profile: "Profile", account: "Sign-in / account",
  verification: "CAPTCHA / OTP",
  email: "Email only", site_error: "Site error", not_registered: "Not sent",
  unconfirmed: "Submitted — confirmation not verified",
  form_rejected: "Form rejected a value", navigation: "Could not continue", review: "Your approval", other: "Other",
};

const SCOPE_LABEL: Record<AnswerScope, string> = {
  auto: "Reuse: automatic", global: "Reuse everywhere", portal: "Only this portal", company: "Only this company",
  job: "Only this job",
};

/** One job waiting for you: what stopped it, exactly what is missing, and the actions that continue it. */
export function NeedsYouCard({ item, onChange }: { item: NeedsYouItem; onChange: () => void }) {
  const open = item.questions.filter((q) => !q.saved_answer && !q.profile_can_answer);
  const [values, setValues] = useState<Record<number, string>>(() =>
    Object.fromEntries(item.questions.map((q) => [q.id, q.saved_answer ?? (q.profile_can_answer ? q.suggestion ?? "" : "")])));
  const [scopes, setScopes] = useState<Record<number, AnswerScope>>({});
  const [result, setResult] = useState<string | null>(null);
  const { busy, error, run } = useAction();

  const act = (action: string, extra: Record<string, unknown> = {}) => async () => {
    if (action === "retry" && item.submit_attempted && !window.confirm(
      "A submit was already attempted for this job. Check on the site that it did not go through, then retry?")) return;
    const res = await run(() => api.jobAction(item.job_id, action, action === "retry" && item.submit_attempted
      ? { force: true, ...extra } : extra));
    if (res) onChange();
  };

  const markApplied = async () => {
    if (!window.confirm("Mark as applied? Only do this if you can see on the site that the application went through.")) return;
    const res = await run(() => api.jobAction(item.job_id, "mark_applied", { note: "Marked as applied by you after checking the site" }));
    if (res) onChange();
  };

  const submitAnswers = async (e: React.FormEvent) => {
    e.preventDefault();
    const answers = item.questions
      .filter((q) => (values[q.id] ?? "").trim() && (values[q.id] ?? "") !== q.saved_answer)
      .map((q) => ({ question: q.question, answer: values[q.id].trim(), scope: scopes[q.id] ?? "auto" }));
    if (!answers.length) return;
    const res = await run(() => api.answerNeedsYou(item.job_id, answers));
    if (res) {
      setResult(res.live ? "Sent to the open application - it continues from the same step."
        : res.requeued ? "Saved - this job is queued again and continues on the next run."
          : "Saved - reused automatically from now on.");
      onChange();
    }
  };

  return (
    <div className="card needs-you">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <div className="row">
            <span className="badge warn">{KIND_LABEL[item.stop_kind] ?? item.stop_kind}</span>
            {item.live && <span className="badge info">Waiting now</span>}
            <span className="muted small">{PORTAL_LABEL[item.portal] ?? item.portal}{item.ats && ` → ${item.ats}`}</span>
          </div>
          <h3 style={{ margin: "6px 0 2px" }}>
            <Link href={`/jobs/${item.job_id}`}>{item.title}</Link> <span className="muted">· {item.company}</span>
          </h3>
          <div className="muted small">Updated {fmtDate(item.updated_at)}{item.step && ` · step: ${item.step}`}</div>
        </div>
        {item.stop_kind === "unconfirmed" ? (
          <div className="row">
            <a className="btn small" href={safeHref(item.final_url || item.url)} target="_blank" rel="noopener noreferrer">
              Open submitted page
            </a>
            <button className="btn small" disabled={busy} onClick={act("verify")}>Retry verification</button>
            <button className="btn small ok" disabled={busy} onClick={markApplied}>Mark as applied</button>
            <button className="btn small" disabled={busy} onClick={act("skip")}>Skip</button>
          </div>
        ) : (
          <div className="row">
            <a className="btn small" href={safeHref(item.final_url || item.url)} target="_blank" rel="noopener noreferrer">
              Open {hostOf(item.final_url || item.url)}
            </a>
            <button className="btn small" disabled={busy} onClick={act("retry")}>Retry</button>
            <button className="btn small" disabled={busy} onClick={act("skip")}>Skip</button>
            <button className="btn small" disabled={busy} onClick={act("park")}>Park</button>
          </div>
        )}
      </div>

      <dl className="kv small" style={{ marginTop: 10 }}>
        <dt>Why it stopped</dt><dd>{item.why}</dd>
        <dt>Exact problem</dt><dd>{item.problem || "—"}</dd>
        <dt>When you continue</dt><dd>{item.resume_note}</dd>
        {!!item.evidence?.length && (<><dt>Evidence seen</dt><dd>{item.evidence.join(" · ")}</dd></>)}
      </dl>

      {!!item.questions.length && (
        <form onSubmit={submitAnswers} style={{ marginTop: 10 }}>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Question</th><th>What is missing</th><th>Your answer</th></tr></thead>
              <tbody>
                {item.questions.map((q) => (
                  <QuestionRow key={q.id} q={q} value={values[q.id] ?? ""} scope={scopes[q.id] ?? "auto"}
                    onValue={(v) => setValues({ ...values, [q.id]: v })}
                    onScope={(s) => setScopes({ ...scopes, [q.id]: s })} />
                ))}
              </tbody>
            </table>
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <button className="btn small primary" disabled={busy}>
              {item.live ? "Answer and continue" : open.length ? "Save answers" : "Save"}
            </button>
            {result && <span className="small" style={{ color: "var(--ok)" }}>{result}</span>}
            {error && <span className="small lvl-error">{error}</span>}
          </div>
        </form>
      )}
    </div>
  );
}

function QuestionRow({ q, value, scope, onValue, onScope }: {
  q: NeedsYouQuestion; value: string; scope: AnswerScope; onValue: (v: string) => void; onScope: (s: AnswerScope) => void;
}) {
  // each choice once: the options' own text is their key (two identical "on" labels used to collide here)
  const choices = uniqueStrings(q.options);
  const status = q.saved_answer ? `Saved answer (${q.saved_scope}) will be used`
    : q.profile_can_answer ? "Your profile answers this now" : q.missing || q.why || "Not in your profile";
  return (
    <tr>
      <td>
        {q.question}
        <div className="muted small">{q.field_type}{q.sensitive && " · only you can answer this"}</div>
      </td>
      <td className="small">
        {status}
        {q.suggestion && !q.saved_answer && !q.profile_can_answer && (
          <div style={{ marginTop: 4 }}>
            Suggestion: <button type="button" className="chip" onClick={() => onValue(q.suggestion ?? "")}>{q.suggestion}</button>
            {q.suggestion_note && <span className="muted"> — {q.suggestion_note}</span>}
          </div>
        )}
      </td>
      <td>
        <div className="row">
          {choices.length ? (
            <select value={value} onChange={(e) => onValue(e.target.value)}>
              <option value="">Choose…</option>
              {choices.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          ) : q.field_type === "textarea" ? (
            <textarea value={value} onChange={(e) => onValue(e.target.value)} rows={3} placeholder="Your answer" />
          ) : (
            <input type="text" value={value} onChange={(e) => onValue(e.target.value)} placeholder="Your answer" />
          )}
          <select className="small" value={scope} onChange={(e) => onScope(e.target.value as AnswerScope)}
            title="Where this answer may be reused">
            {(Object.keys(SCOPE_LABEL) as AnswerScope[]).map((s) => <option key={s} value={s}>{SCOPE_LABEL[s]}</option>)}
          </select>
        </div>
      </td>
    </tr>
  );
}
