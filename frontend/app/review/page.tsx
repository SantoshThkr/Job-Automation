"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { GateBanner } from "@/components/GateBanner";
import { NeedsYouCard } from "@/components/NeedsYouCard";
import { Score } from "@/components/StatusBadge";
import { JobSummary, api } from "@/lib/api";
import { useAction, usePoll } from "@/lib/hooks";
import { PORTAL_LABEL, uniqueById } from "@/lib/format";

export default function ReviewPage() {
  const [showParked, setShowParked] = useState(false);
  const agent = usePoll(api.agent, 2000);
  const decide = usePoll(() => api.jobs({ status: "REVIEW_PENDING", sort: "score", page_size: 100 }), 5000);
  const needs = usePoll(() => api.needsYou(showParked), 5000, [showParked]);
  const refresh = () => { agent.refresh(); decide.refresh(); needs.refresh(); };

  // One card per job: job_id is the record's primary key (the API returns each waiting job once). Should it ever
  // repeat an id, that is the same record twice - shown once and reported, not hidden.
  const { items, duplicates } = uniqueById(needs.data?.items ?? [], (it) => it.job_id);
  const repeated = duplicates.join(",");
  useEffect(() => {
    if (repeated) console.warn(`/api/needs-you returned the same job more than once (job_id ${repeated})`);
  }, [repeated]);
  const nothing = !agent.data?.gate_kind && !decide.data?.items.length && !items.length;
  const kinds = Object.entries(needs.data?.by_kind ?? {}).sort((a, b) => b[1] - a[1]);
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Needs you</h1>
          <p className="sub">Only what the assistant cannot do truthfully on its own: each item says what is missing and what happens when you answer.</p>
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

      {(!!items.length || !!needs.data?.parked) && (
        <div className="row" style={{ justifyContent: "space-between", margin: "8px 0" }}>
          <div className="row small">
            <strong>{items.length} application(s) waiting</strong>
            {kinds.map(([k, n]) => <span key={k} className="chip">{k.replace("_", " ")}: {n}</span>)}
          </div>
          {!!needs.data?.parked && (
            <label className="small row">
              <input type="checkbox" checked={showParked} onChange={(e) => setShowParked(e.target.checked)} />
              Show {needs.data.parked} parked
            </label>
          )}
        </div>
      )}
      {items.map((it) => <NeedsYouCard key={it.job_id} item={it} onChange={refresh} />)}
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
