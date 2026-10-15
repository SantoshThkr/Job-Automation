"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { AnswerForm } from "@/components/AnswerForm";
import { GateBanner } from "@/components/GateBanner";
import { DecisionBadge, Score, StatusBadge } from "@/components/StatusBadge";
import { JobDetail, QA, api } from "@/lib/api";
import { useAction, usePoll } from "@/lib/hooks";
import { MODE_LABEL, PORTAL_LABEL, fmtDate, ANSWER_MODE, safeHref, sourceSummary, uniqueStrings } from "@/lib/format";

const BREAKDOWN_LABEL: Record<string, string> = {
  title: "Role title",
  frontend_ratio: "Frontend focus",
  skills: "Skills",
  experience: "Experience",
  seniority: "Seniority",
  location: "Location",
};

export default function JobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const job = usePoll(() => api.job(id), 4000, [id]);
  const agent = usePoll(api.agent, 2500);
  const { busy, error, run } = useAction();
  const [useAi, setUseAi] = useState(false);
  const j = job.data;

  if (job.error && !j) return <div className="alert err">Could not load job: {job.error}</div>;
  if (!j) return <p className="muted">Loading…</p>;

  const app = j.application;
  const act = (action: string, extra: Record<string, unknown> = {}) => async () => {
    if (action === "mark_applied" && !window.confirm("Mark this job as applied (you applied yourself)?")) return;
    let res = await run(() => api.jobAction(j.id, action, extra));
    if (!res && action === "retry" && app?.submit_attempted && !app.confirmed &&
        window.confirm("A submit was already attempted for this job. Check the portal first - retry anyway?")) {
      res = await run(() => api.jobAction(j.id, action, { force: true }));
    }
    job.refresh();
  };
  const canApprove = ["REVIEW_PENDING", "SKIPPED", "NEW", "DRY_RUN"].includes(j.status);
  const canRetry = ["FAILED", "MANUAL_ACTION_REQUIRED", "DRY_RUN"].includes(j.status);
  const final = ["APPLIED", "ALREADY_APPLIED"].includes(j.status);

  return (
    <>
      <div className="page-head">
        <div>
          <p className="sub"><Link href="/jobs">← Jobs</Link></p>
          <h1>{j.title}</h1>
          <p className="sub">
            {j.company} · {j.location || "location n/a"} · {PORTAL_LABEL[j.portal] ?? j.portal}
          </p>
        </div>
        <div className="row">
          <StatusBadge status={j.status} />
          <DecisionBadge decision={j.decision} />
          <span>Match <Score value={j.match_score} /></span>
        </div>
      </div>

      {agent.data?.pending_job_id === j.id && <GateBanner agent={agent.data} onChange={() => { agent.refresh(); job.refresh(); }} />}

      {j.status_reason && (
        <div className={`alert ${j.status === "FAILED" ? "err" : j.status === "APPLIED" ? "info" : "warn"}`}>
          <div className="title">Status: {j.status.replaceAll("_", " ").toLowerCase()}</div>
          <p>{j.status_reason}</p>
        </div>
      )}

      <div className="card row">
        <button className="btn primary" disabled={busy || !canApprove} onClick={act("approve")}>Approve (queue to apply)</button>
        <button className="btn" disabled={busy || final || j.status === "SKIPPED"} onClick={act("skip")}>Skip</button>
        <button className="btn" disabled={busy || !canRetry} onClick={act("retry")}>Retry</button>
        <button className="btn" disabled={busy || j.status === "APPLIED"} onClick={act("mark_applied")}>Mark as applied</button>
        <span style={{ flex: 1 }} />
        <label className="check small"><input type="checkbox" checked={useAi} onChange={(e) => setUseAi(e.target.checked)} /> use AI</label>
        <button className="btn" disabled={busy} onClick={async () => { await run(() => api.reevaluate(j.id, useAi)); job.refresh(); }}>
          Re-evaluate
        </button>
        <a className="btn" href={safeHref(j.url)} target="_blank" rel="noopener noreferrer">Open on {PORTAL_LABEL[j.portal]}</a>
        {error && <span className="small lvl-error">{error}</span>}
      </div>

      <div className="grid grid-2">
        <div>
          <JobInfo j={j} />
          <MatchExplanation j={j} />
          <div className="card">
            <h2>Job description</h2>
            {j.description ? <div className="jd">{j.description}</div> : <p className="muted">Not loaded.</p>}
          </div>
        </div>
        <div>
          <div className="card">
            <h2>Application</h2>
            {!app ? <p className="muted">No application attempt yet.</p> : (
              <dl className="kv">
                <dt>Status</dt><dd><StatusBadge status={app.status} /></dd>
                <dt>Mode</dt><dd>{MODE_LABEL[app.mode] ?? app.mode}</dd>
                <dt>Resume used</dt><dd>{app.resume_id ?? "—"}{app.resume_path && <div className="muted small">{app.resume_path}</div>}</dd>
                <dt>Applied via</dt><dd>{app.apply_target === "external" ? `Company site (${(app.ats_platform ?? "unknown").replace(/^site:/, "")})` : PORTAL_LABEL[j.portal]}</dd>
                <dt>Attempts</dt><dd>{app.attempts}{app.steps ? ` · ${app.steps} browser step(s) last time` : ""}</dd>
                <dt>Submitted</dt><dd>{app.submit_attempted ? (app.confirmed ? "Yes - confirmed" : "Submit clicked - not confirmed") : "No"}</dd>
                {app.applied_at && <><dt>Applied at</dt><dd>{fmtDate(app.applied_at)}</dd></>}
                {app.confirmation_text && <><dt>Confirmation</dt><dd>{app.confirmation_text}</dd></>}
                {app.manual_action_reason && <><dt>Manual action</dt><dd className="lvl-warning" style={{ whiteSpace: "pre-wrap" }}>{app.manual_action_reason}</dd></>}
                {app.failure_reason && <><dt>Failure reason</dt><dd className="lvl-error" style={{ whiteSpace: "pre-wrap" }}>{app.failure_reason}</dd></>}
                {app.resume_url && <><dt>Continues from</dt><dd className="small"><a href={safeHref(app.resume_url)} target="_blank" rel="noopener noreferrer">{app.resume_url}</a><div className="muted">Retry picks the application up on this page instead of starting over.</div></dd></>}
                {app.final_url && <><dt>Last URL</dt><dd className="small"><a href={safeHref(app.final_url)} target="_blank" rel="noopener noreferrer">{app.final_url}</a></dd></>}
              </dl>
            )}
          </div>
          <Questions answers={app?.answers ?? []} onSaved={job.refresh} />
          <div className="card">
            <h2>Timeline</h2>
            {!j.events.length && <p className="muted">No events.</p>}
            <ul className="timeline">
              {j.events.map((e) => (
                <li key={e.id}>
                  <span className="when">{fmtDate(e.ts)}</span>
                  <span className={`small lvl-${e.level}`}>
                    <strong>{e.type.replaceAll("_", " ")}</strong> - {e.message}
                    {e.screenshot && <> · <a href={safeHref(e.screenshot)} target="_blank" rel="noopener noreferrer">screenshot</a></>}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </>
  );
}

function JobInfo({ j }: { j: JobDetail }) {
  return (
    <div className="card">
      <h2>Job information</h2>
      <dl className="kv">
        <dt>Company</dt><dd>{j.company || "—"}</dd>
        <dt>Role</dt><dd>{j.title}</dd>
        <dt>Location</dt><dd>{j.location || "—"}</dd>
        <dt>Experience</dt><dd>{j.experience_text || "—"}</dd>
        <dt>Salary</dt><dd>{j.salary_text || "—"}</dd>
        <dt>Posted</dt>
        <dd data-testid="job-posted">
          {j.posted_label || "Unknown - the portal did not show when it was posted"}
          {j.posted_text && j.posted_source !== "page" && <div className="muted small">Listing said &ldquo;{j.posted_text}&rdquo; when found</div>}
          {j.closed_reason && <div className="small lvl-error">Closed: {j.closed_reason}</div>}
          {j.recency && j.recency !== "in" && j.recency_reason && (
            <div className={`small ${j.recency === "out" ? "lvl-error" : "lvl-warning"}`}>
              {j.recency === "out" ? "Not applied: " : "Not applied automatically: "}{j.recency_reason}
            </div>
          )}
        </dd>
        <dt>Apply type</dt><dd>{j.apply_type}{j.external_url && <> · <a href={safeHref(j.external_url)} target="_blank" rel="noopener noreferrer">company site</a></>}</dd>
        <dt>Found via</dt><dd>{PORTAL_LABEL[j.portal]}{j.search_query && ` search "${j.search_query}"`} · {fmtDate(j.discovered_at)}</dd>
        {!!j.skills.length && <><dt>Portal skills</dt><dd><div className="chips">{uniqueStrings(j.skills).map((s) => <span key={s} className="chip">{s}</span>)}</div></dd></>}
      </dl>
    </div>
  );
}

function MatchExplanation({ j }: { j: JobDetail }) {
  const m = j.match_details;
  if (!m || m.score === undefined) return <div className="card"><h2>Match explanation</h2><p className="muted">Not evaluated yet.</p></div>;
  const ai = m.ai as Record<string, any> | null | undefined;
  return (
    <div className="card">
      <div className="spread"><h2>Match explanation</h2><span>Score <Score value={m.score} /> <DecisionBadge decision={m.decision} /></span></div>
      <div className="breakdown" style={{ marginBottom: 12 }}>
        {Object.entries(m.breakdown ?? {}).map(([k, v]) => (
          <div key={k} style={{ display: "contents" }}>
            <span className="small muted">{BREAKDOWN_LABEL[k] ?? k}</span>
            <div className="bar"><span style={{ width: `${Math.max(2, v)}%` }} /></div>
            <span className="small num">{Math.round(v)}</span>
          </div>
        ))}
      </div>
      {m.frontend_ratio !== null && m.frontend_ratio !== undefined && (
        <p className="small">Frontend share of the role: <strong>{Math.round(m.frontend_ratio * 100)}%</strong> · category: {m.category}
          {(() => {
            const a = (m.analysis ?? {}) as { frontend_skills?: string[]; backend_skills?: string[] };
            return a.frontend_skills || a.backend_skills ? (
              <span className="muted"> · the description names {a.frontend_skills?.length ? `frontend: ${a.frontend_skills.slice(0, 5).join(", ")}` : "no frontend technology"}
                {a.backend_skills?.length ? `; backend/other: ${a.backend_skills.slice(0, 5).join(", ")}` : ""}</span>
            ) : null;
          })()}
        </p>
      )}
      {m.decided_by && <p className="small" data-testid="decided-by">Decided by: <strong>{m.decided_by}</strong></p>}
      {!!m.codes?.length && (
        <div className="chips" data-testid="reason-codes" style={{ marginBottom: 8 }}>
          {m.codes.map((c, i) => <span key={`${c.code}-${i}`} className={`chip ${c.kind === "blocker" ? "miss" : ""}`} title={c.text}>{c.kind}: {c.code}</span>)}
        </div>
      )}
      {!!m.blockers?.length && <><h3 className="lvl-error">Blockers</h3><ul className="list small">{uniqueStrings(m.blockers).map((b) => <li key={b}>{b}</li>)}</ul></>}
      {!!m.flags?.length && <><h3 className="lvl-warning">Needs a human look</h3><ul className="list small">{uniqueStrings(m.flags).map((b) => <li key={b}>{b}</li>)}</ul></>}
      {!!m.reasons?.length && <><h3>Why</h3><ul className="list small">{uniqueStrings(m.reasons).map((b) => <li key={b}>{b}</li>)}</ul></>}
      {(!!m.matched_skills?.length || !!m.missing_skills?.length) && (
        <>
          <h3>Skills</h3>
          <div className="chips">
            {uniqueStrings(m.matched_skills).map((s) => <span key={s} className="chip">✓ {s}</span>)}
            {uniqueStrings(m.missing_skills).map((s) => <span key={s} className="chip miss">missing: {s}</span>)}
          </div>
        </>
      )}
      <h3>Local AI</h3>
      {ai?.used ? (
        <p className="small">
          {String(ai.summary ?? "")} - recommends <strong>{String(ai.recommendation)}</strong>, relevance {String(ai.relevance_score)},
          frontend {String(ai.frontend_percentage)}%, confidence {Number(ai.confidence).toFixed(2)} ({String(ai.effect ?? "")})
        </p>
      ) : <p className="small muted">{String(ai?.reason ?? "Not used")}</p>}
      {m.resume_id && <p className="small">Recommended resume: <strong>{m.resume_id}</strong></p>}
    </div>
  );
}

function Questions({ answers, onSaved }: { answers: QA[]; onSaved: () => void }) {
  if (!answers.length) return <div className="card"><h2>Questions & answers</h2><p className="muted">No questions recorded.</p></div>;
  return (
    <div className="card">
      <h2>Questions & answers</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Question</th><th>Answer</th><th className="num">Conf.</th><th>How it was answered</th></tr></thead>
          <tbody>
            {answers.map((qa) => (
              <tr key={qa.id}>
                <td className="small">{qa.question}{qa.required && <span className="lvl-error"> *</span>}
                  {qa.note && <div className="muted small">{qa.note}</div>}</td>
                <td className="small">
                  {qa.answer ?? <span className="muted">—</span>}
                  {qa.needs_review && <AnswerForm qa={qa} onSaved={onSaved} />}
                </td>
                <td className="num small">{qa.confidence.toFixed(2)}</td>
                <td className="small">
                  {qa.needs_review ? <span className="badge warn">needs you</span>
                    : <span className={`badge ${qa.mode === "ai_generated" || qa.mode === "composed" ? "info" : "ok"}`}>{ANSWER_MODE[qa.mode ?? ""] ?? qa.source}</span>}
                  {qa.verified === true && <span className="muted"> · verified</span>}
                  {qa.verified === false && <span className="lvl-warning"> · not verified</span>}
                  {sourceSummary(qa.sources) && <div className="muted">from {sourceSummary(qa.sources)}</div>}
                  {qa.category && <div className="muted">{qa.category.replaceAll("_", " ").toLowerCase()}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
