"use client";

import Link from "next/link";
import { AnswerForm } from "@/components/AnswerForm";
import { GateBanner } from "@/components/GateBanner";
import { Score, StatusBadge } from "@/components/StatusBadge";
import { JobSummary, api } from "@/lib/api";
import { useAction, usePoll } from "@/lib/hooks";
import { PORTAL_LABEL, fmtDate } from "@/lib/format";

export default function ReviewPage() {
  const agent = usePoll(api.agent, 2000);
  const decide = usePoll(() => api.jobs({ status: "REVIEW_PENDING", sort: "score", page_size: 100 }), 5000);
  const manual = usePoll(() => api.jobs({ status: "MANUAL_ACTION_REQUIRED,AWAITING_APPROVAL", sort: "updated", page_size: 100 }), 5000);
  const questions = usePoll(api.pendingQuestions, 6000);
  const refresh = () => { agent.refresh(); decide.refresh(); manual.refresh(); questions.refresh(); };

  const nothing = !agent.data?.gate_kind && !decide.data?.items.length && !manual.data?.items.length && !questions.data?.length;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Needs you</h1>
          <p className="sub">Decisions, answers and manual steps the assistant will not guess.</p>
        </div>
      </div>
      <GateBanner agent={agent.data} onChange={refresh} />
      {nothing && <div className="card empty">All clear - nothing needs your attention.</div>}

      {!!decide.data?.items.length && (
        <div className="card">
          <h2>Should I apply? ({decide.data.total})</h2>
          <p className="muted small">The matcher was not sure about these. Approved jobs are applied to on the next run.</p>
          <DecisionTable jobs={decide.data.items} onDone={refresh} />
        </div>
      )}

      {!!questions.data?.length && (
        <div className="card">
          <h2>Questions to answer ({questions.data.length})</h2>
          <p className="muted small">Your answer is saved and reused automatically on future forms. Then press Retry on the job.</p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Question</th><th>Asked by</th><th>Your answer</th></tr></thead>
              <tbody>
                {questions.data.map((qa) => (
                  <tr key={qa.id}>
                    <td>{qa.question}{qa.note && <div className="muted small">{qa.note}</div>}</td>
                    <td className="small"><Link href={`/jobs/${qa.job_id}`}>{qa.job_company} · {qa.job_title}</Link></td>
                    <td><AnswerForm qa={qa} onSaved={refresh} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!!manual.data?.items.length && (
        <div className="card">
          <h2>Manual action required ({manual.data.total})</h2>
          <ManualTable jobs={manual.data.items} onDone={refresh} />
        </div>
      )}
    </>
  );
}

function DecisionTable({ jobs, onDone }: { jobs: JobSummary[]; onDone: () => void }) {
  const { busy, run } = useAction();
  const act = (id: number, action: string) => async () => { await run(() => api.jobAction(id, action)); onDone(); };
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Company</th><th>Role</th><th>Portal</th><th className="num">Match</th><th>Why unsure</th><th /></tr></thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id}>
              <td>{j.company}</td>
              <td><Link href={`/jobs/${j.id}`}>{j.title}</Link><div className="muted small">{j.location}</div></td>
              <td>{PORTAL_LABEL[j.portal]}</td>
              <td className="num"><Score value={j.match_score} /></td>
              <td className="small">{j.status_reason}</td>
              <td><div className="row">
                <button className="btn small ok" disabled={busy} onClick={act(j.id, "approve")}>Apply</button>
                <button className="btn small" disabled={busy} onClick={act(j.id, "skip")}>Skip</button>
              </div></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ManualTable({ jobs, onDone }: { jobs: JobSummary[]; onDone: () => void }) {
  const { busy, run, error } = useAction();
  const act = (id: number, action: string, extra = {}) => async () => {
    if (action === "mark_applied" && !window.confirm("Mark as applied (you finished it yourself)?")) return;
    await run(() => api.jobAction(id, action, extra));
    onDone();
  };
  return (
    <>
      {error && <p className="small lvl-error">{error}</p>}
      <div className="table-wrap">
        <table>
          <thead><tr><th>Company</th><th>Role</th><th>Status</th><th>What happened</th><th>Updated</th><th /></tr></thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id}>
                <td>{j.company}</td>
                <td><Link href={`/jobs/${j.id}`}>{j.title}</Link><div className="muted small">{PORTAL_LABEL[j.portal]}{j.ats_platform && ` → ${j.ats_platform}`}</div></td>
                <td><StatusBadge status={j.status} /></td>
                <td className="small">{j.status_reason}</td>
                <td className="small">{fmtDate(j.updated_at)}</td>
                <td><div className="row">
                  <a className="btn small" href={j.url} target="_blank" rel="noreferrer">Open</a>
                  <button className="btn small" disabled={busy} onClick={act(j.id, "retry")}>Retry</button>
                  <button className="btn small" disabled={busy} onClick={act(j.id, "mark_applied")}>Applied</button>
                  <button className="btn small" disabled={busy} onClick={act(j.id, "skip")}>Skip</button>
                </div></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
