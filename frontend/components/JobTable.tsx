"use client";

import { useRouter } from "next/navigation";
import { JobSummary } from "@/lib/api";
import { PORTAL_LABEL, fmtDate, postedAge } from "@/lib/format";
import { Score, StatusBadge } from "./StatusBadge";

// statuses whose reason answers "why was this not applied to?" - shown in the row, not only in a tooltip
const EXPLAINED = new Set(["SKIPPED", "REVIEW_PENDING", "MANUAL_ACTION_REQUIRED", "FAILED", "DUPLICATE", "EXPIRED"]);

/** When the job was posted (never when it was found) and where it stands against the job-recency window. */
export function PostedCell({ job }: { job: JobSummary }) {
  if (!job.posted_at) {
    return <span className="muted" title={job.recency_reason || "The portal did not show when this job was posted"}>Unknown</span>;
  }
  const day = fmtDate(job.posted_at, false); // a date-only posting is stored at noon, so its day never shifts
  const flag = job.closed_reason ? "Closed" : job.recency === "out" ? "Too old" : job.recency === "unknown" ? "Unsure" : "";
  return (
    <span title={[job.posted_label, job.closed_reason, job.recency_reason].filter(Boolean).join(" · ")}>
      <span data-testid="posted-age">{postedAge(job.posted_age_days, job.posted_open_ended)}</span>
      <div className="muted">{job.posted_open_ended ? `on/before ${day}` : day}</div>
      {flag && <span className={`badge ${flag === "Unsure" ? "warn" : "err"}`} data-testid="posted-flag">{flag}</span>}
    </span>
  );
}

export function JobTable({ jobs, compact = false }: { jobs: JobSummary[]; compact?: boolean }) {
  const router = useRouter();
  if (!jobs.length) return <div className="empty">No jobs yet.</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Company</th>
            <th>Role</th>
            <th>Portal</th>
            {!compact && <th>Location</th>}
            <th className="num">Match</th>
            <th>Status</th>
            <th>Posted</th>
            <th>{compact ? "Date" : "Updated"}</th>
            {!compact && <th>Resume</th>}
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id} className="click" onClick={() => router.push(`/jobs/${j.id}`)}>
              <td className="ellipsis" title={j.company}>{j.company || "—"}</td>
              <td className="ellipsis" title={j.title}>
                {j.title}
                {j.ats_platform && <span className="muted small"> · via {j.ats_platform}</span>}
              </td>
              <td>{PORTAL_LABEL[j.portal] ?? j.portal}</td>
              {!compact && <td className="ellipsis" title={j.location}>{j.location || "—"}</td>}
              <td className="num"><Score value={j.match_score} /></td>
              <td title={j.status_reason}>
                <StatusBadge status={j.status} />
                {EXPLAINED.has(j.status) && j.status_reason && (
                  <div className="reason small muted" data-testid="status-reason">{j.status_reason}</div>
                )}
              </td>
              <td className="small"><PostedCell job={j} /></td>
              <td className="small">{fmtDate(j.applied_at || j.updated_at)}</td>
              {!compact && <td className="small">{j.resume_id ?? "—"}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
