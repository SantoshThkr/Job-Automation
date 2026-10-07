"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { JobTable } from "@/components/JobTable";
import { api } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { PORTAL_LABEL, STATUS_LABEL } from "@/lib/format";

const PAGE_SIZE = 50;

function JobsInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [status, setStatus] = useState(params.get("status") ?? "");
  const [portal, setPortal] = useState(params.get("portal") ?? "");
  const [decision, setDecision] = useState(params.get("decision") ?? "");
  const [q, setQ] = useState(params.get("q") ?? "");
  const [appsOnly, setAppsOnly] = useState(params.get("applications_only") === "true");
  const [sort, setSort] = useState(params.get("sort") ?? "date");
  const [page, setPage] = useState(1);
  const [debouncedQ, setDebouncedQ] = useState(q);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 300);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    const next = new URLSearchParams();
    if (status) next.set("status", status);
    if (portal) next.set("portal", portal);
    if (decision) next.set("decision", decision);
    if (debouncedQ) next.set("q", debouncedQ);
    if (appsOnly) next.set("applications_only", "true");
    if (sort !== "date") next.set("sort", sort);
    router.replace(`/jobs${next.size ? `?${next}` : ""}`, { scroll: false });
    setPage(1);
  }, [status, portal, decision, debouncedQ, appsOnly, sort, router]);

  const { data, error } = usePoll(
    () => api.jobs({ status, portal, decision, q: debouncedQ, applications_only: appsOnly, sort, page, page_size: PAGE_SIZE }),
    6000,
    [status, portal, decision, debouncedQ, appsOnly, sort, page],
  );
  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Jobs</h1>
          <p className="sub">{data ? `${data.total} job(s)` : "Loading…"}{error && <span className="lvl-error"> · {error}</span>}</p>
        </div>
      </div>
      <div className="card row">
        <input type="search" placeholder="Search company, role, location" value={q} onChange={(e) => setQ(e.target.value)} style={{ minWidth: 240 }} />
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          <option value="">All statuses</option>
          {Object.entries(STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          <option value="SKIPPED,DUPLICATE">Skipped + duplicates</option>
        </select>
        <select value={portal} onChange={(e) => setPortal(e.target.value)} aria-label="Portal">
          <option value="">All portals</option>
          {Object.entries(PORTAL_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select value={decision} onChange={(e) => setDecision(e.target.value)} aria-label="Decision">
          <option value="">Any decision</option>
          <option value="APPLY">Apply</option>
          <option value="MANUAL_REVIEW">Review</option>
          <option value="SKIP">Skip</option>
        </select>
        <select value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort">
          <option value="date">Newest</option>
          <option value="updated">Recently updated</option>
          <option value="score">Match score</option>
          <option value="company">Company</option>
        </select>
        <label className="check small"><input type="checkbox" checked={appsOnly} onChange={(e) => setAppsOnly(e.target.checked)} /> Applications only</label>
      </div>
      <JobTable jobs={data?.items ?? []} />
      {pages > 1 && (
        <div className="pager">
          <button className="btn small" disabled={page <= 1} onClick={() => setPage(page - 1)}>← Prev</button>
          <span className="small muted">Page {page} / {pages}</span>
          <button className="btn small" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next →</button>
        </div>
      )}
    </>
  );
}

export default function JobsPage() {
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <JobsInner />
    </Suspense>
  );
}
