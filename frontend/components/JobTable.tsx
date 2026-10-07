"use client";

import { useRouter } from "next/navigation";
import { JobSummary } from "@/lib/api";
import { PORTAL_LABEL, fmtDate } from "@/lib/format";
import { Score, StatusBadge } from "./StatusBadge";

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
            <th>Date</th>
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
              <td title={j.status_reason}><StatusBadge status={j.status} /></td>
              <td className="small">{fmtDate(j.applied_at || j.updated_at)}</td>
              {!compact && <td className="small">{j.resume_id ?? "—"}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
