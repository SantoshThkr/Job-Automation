"use client";

import Link from "next/link";
import { AgentControls } from "@/components/AgentControls";
import { GateBanner } from "@/components/GateBanner";
import { JobTable } from "@/components/JobTable";
import { Portal, api } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { MODE_LABEL, timeAgo } from "@/lib/format";

const STAT_CARDS: { key: string; label: string; href: string }[] = [
  { key: "jobs_discovered", label: "Jobs discovered", href: "/jobs" },
  { key: "relevant_jobs", label: "Relevant jobs", href: "/jobs?decision=APPLY" },
  { key: "applications", label: "Applications", href: "/jobs?status=APPLIED" },
  { key: "skipped", label: "Skipped", href: "/jobs?status=SKIPPED,DUPLICATE" },
  { key: "failed", label: "Failed", href: "/jobs?status=FAILED" },
  { key: "manual_action_required", label: "Manual action required", href: "/review" },
  { key: "today_applications", label: "Today's applications", href: "/jobs?status=APPLIED" },
  { key: "weekly_applications", label: "Weekly applications", href: "/jobs?status=APPLIED" },
];

export default function DashboardPage() {
  const stats = usePoll(api.stats, 4000);
  const agent = usePoll(api.agent, 2000);
  const events = usePoll(() => api.events(30), 4000);
  const profile = usePoll(api.profile, 15000);
  const recent = usePoll(() => api.jobs({ applications_only: true, sort: "updated", page_size: 8 }), 5000);

  const refreshAll = () => {
    agent.refresh();
    stats.refresh();
    events.refresh();
  };
  const s = stats.data as unknown as Record<string, number> | null;
  const blocking = profile.data?.issues.filter((i) => i.severity === "error") ?? [];
  const defaultPortals = (profile.data?.profile?.search?.portals ?? ["naukri", "linkedin", "instahyre"]) as Portal[];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p className="sub">
            Mode: <strong>{agent.data ? MODE_LABEL[agent.data.mode] : "…"}</strong>
            {stats.error && <span className="lvl-error"> · backend unreachable ({stats.error})</span>}
          </p>
        </div>
      </div>

      {profile.data && !profile.data.ready_for_submission && (
        <div className="alert warn">
          <div className="title">Profile not ready for real submissions</div>
          <p>
            {blocking.length} item(s) to fix in <code>config/profile.json</code> - Dry Run still works.{" "}
            <Link href="/settings">See details →</Link>
          </p>
        </div>
      )}

      <GateBanner agent={agent.data} onChange={refreshAll} />

      <div className="stats">
        {STAT_CARDS.map((c) => (
          <Link key={c.key} href={c.href} className="stat">
            <div className="label">{c.label}</div>
            <div className="value">{s ? s[c.key] ?? 0 : "…"}</div>
          </Link>
        ))}
      </div>

      <div className="grid grid-3">
        <div>
          <AgentControls agent={agent.data} onChange={refreshAll} defaultPortals={defaultPortals} />
          <div className="card">
            <div className="spread" style={{ marginBottom: 10 }}>
              <h2 style={{ margin: 0 }}>Recent applications</h2>
              <Link href="/jobs?applications_only=true" className="small">All →</Link>
            </div>
            <JobTable jobs={recent.data?.items ?? []} compact />
          </div>
        </div>
        <div className="card" style={{ maxHeight: 760, overflowY: "auto" }}>
          <h2>Activity</h2>
          {!events.data?.length && <div className="empty">Nothing yet - press Start.</div>}
          <ul className="timeline">
            {events.data?.map((e) => (
              <li key={e.id}>
                <span className="when">{timeAgo(e.ts)}</span>
                <span className={`small lvl-${e.level}`}>
                  {e.job_id ? <Link href={`/jobs/${e.job_id}`}>{e.job_company || e.job_title || `Job ${e.job_id}`}</Link> : null}
                  {e.job_id ? ": " : ""}
                  {e.message}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </>
  );
}
