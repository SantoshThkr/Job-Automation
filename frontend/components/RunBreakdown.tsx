"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { RunBreakdown as Breakdown, api } from "@/lib/api";
import { PORTAL_LABEL } from "@/lib/format";

const OUTCOME_LABEL: Record<string, string> = {
  relevant: "Relevant (queued)", review: "Your decision", skipped: "Skipped after reading", skipped_title: "Skipped from the title",
  too_old: "Too old", closed: "Closed", duplicate: "Duplicate", error: "Could not be read",
};
const QUEUE_LABEL: Record<string, string> = {
  applied: "Applied", dry_run: "Dry run", needs_you: "Needs you", failed: "Failed", closed: "Closed",
  set_aside_by_date: "Queued earlier, now too old", date_unknown: "Queued earlier, date unknown", already_applied: "Already applied",
};

/** One run, reconciled: what was seen once vs again, what was decided and why, by which rule and stage. */
export function RunBreakdown({ runId }: { runId: number }) {
  const [data, setData] = useState<Breakdown | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [auditing, setAuditing] = useState(false);

  useEffect(() => {
    let live = true;
    setData(null);
    setError(null);
    api.runBreakdown(runId).then((d) => live && setData(d)).catch((e) => live && setError(String(e?.message ?? e)));
    return () => { live = false; };
  }, [runId]);

  async function runAudit() {
    setAuditing(true);
    try {
      setData(await api.runBreakdown(runId, true));
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setAuditing(false);
    }
  }

  if (error) return <p className="small lvl-error">{error}</p>;
  if (!data) return <p className="small muted">Loading run #{runId}…</p>;
  const f = data.funnel;
  const d = data.discovery;
  return (
    <div className="run-breakdown" data-testid="run-breakdown">
      <h3>Run #{data.run.id}: what it saw</h3>
      <ul className="list small">
        <li><strong>{f.sightings}</strong> listing sightings (a job listed by two searches counts twice)
          {f.unique_listings != null && <> · <strong>{f.unique_listings}</strong> unique listings ({f.repeat_sightings} repeats)</>}</li>
        <li><strong>{f.new_records}</strong> new to the assistant · {f.known_sightings} sightings of jobs it already knew
          {f.read_again ? ` · ${f.read_again} read again` : ""}{f.already_applied_on_portal ? ` · ${f.already_applied_on_portal} already applied` : ""}</li>
        {!!f.searches_skipped_redundant && <li>{f.searches_skipped_redundant} search(es) skipped: the portal kept listing the same jobs</li>}
        <li className={data.reconciles ? "muted" : "lvl-warning"} data-testid="reconciliation">
          {data.reconciles ? "Reconciles: " : "Does not reconcile: "}{data.reconciliation}</li>
      </ul>

      <h3>{d.jobs} job(s) decided in this run</h3>
      <div className="table-wrap">
        <table className="runs">
          <thead><tr><th>Outcome</th><th className="num">All</th>{Object.keys(d.by_portal).map((p) => <th key={p} className="num">{PORTAL_LABEL[p] ?? p}</th>)}</tr></thead>
          <tbody>
            {Object.entries(d.by_outcome).map(([k, n]) => (
              <tr key={k}>
                <td>{OUTCOME_LABEL[k] ?? k}</td><td className="num">{n}</td>
                {Object.entries(d.by_portal).map(([p, c]) => <td key={p} className="num">{c[k] ?? 0}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="small muted">
        Stage: {Object.entries(d.by_stage).map(([k, n]) => `${n} ${k}`).join(" · ") || "—"}
        {" · "}Decided by: {Object.entries(d.decided_by).map(([k, n]) => `${n} ${k}`).join(" · ") || "—"}
      </p>
      {!!Object.keys(data.queue).length && (
        <p className="small">Queue: {Object.entries(data.queue).map(([k, n]) => `${n} ${QUEUE_LABEL[k] ?? k}`).join(" · ")}</p>
      )}

      <h3>Why (rule → jobs)</h3>
      <ul className="list small" data-testid="run-reasons">
        {d.reasons.map((r) => (
          <li key={`${r.outcome}-${r.code}`}>
            <strong>{r.count}</strong> {OUTCOME_LABEL[r.outcome] ?? r.outcome} · <code>{r.code}</code>{r.meaning && <span className="muted"> - {r.meaning}</span>}
            {!!r.examples.length && (
              <div className="muted">e.g. {r.examples.slice(0, 3).map((e, i) => (
                <span key={e.id}>{i ? ", " : ""}<Link href={`/jobs/${e.id}`} title={e.why}>{e.title}</Link></span>
              ))}</div>
            )}
          </li>
        ))}
      </ul>

      {data.audit ? (
        <p className="small" data-testid="run-audit">
          Audit by today&apos;s rules of the run&apos;s {data.audit.skips_checked} skips: {data.audit.still_skipped} still skipped ·{" "}
          <strong>{data.audit.would_apply}</strong> would now be applied to · <strong>{data.audit.would_put_to_you}</strong> put to you ·{" "}
          <strong>{data.audit.would_read_first}</strong> read before deciding (possible false skips - to check, not errors)
          {!!data.audit.examples.length && <span className="muted"> - e.g. {data.audit.examples.slice(0, 4).map((e) => e.title).join(", ")}</span>}
        </p>
      ) : (
        <button className="btn small" disabled={auditing} onClick={runAudit}>{auditing ? "Auditing…" : "Audit the skips with today's rules"}</button>
      )}
    </div>
  );
}
