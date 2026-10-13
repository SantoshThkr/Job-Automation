"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useAction, usePoll } from "@/lib/hooks";
import { PORTAL_LABEL } from "@/lib/format";
import { SavedAnswers } from "@/components/SavedAnswers";

const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

export default function SettingsPage() {
  const profile = usePoll(api.profile, 0);
  const resumes = usePoll(api.resumes, 0);
  const ai = usePoll(api.aiStatus, 0);
  const memory = usePoll(api.memory, 0);
  const p = profile.data?.profile;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="sub">Candidate facts live in <code>{profile.data?.path ?? "config/profile.json"}</code> - edit that file for personal details.</p>
        </div>
      </div>

      {profile.data && (
        <div className={`alert ${profile.data.ready_for_submission ? "info" : "warn"}`}>
          <div className="title">{profile.data.ready_for_submission ? "Profile ready for real submissions" : "Profile needs attention before real submissions"}</div>
          {profile.data.load_error && <p className="lvl-error">{profile.data.load_error}</p>}
          <ul className="list small">
            {profile.data.issues.map((i) => (
              <li key={i.field + i.message} className={i.severity === "error" ? "lvl-error" : ""}>
                <strong>{i.severity === "error" ? "Must fix" : "Optional"}:</strong> {i.message} <code>{i.field}</code>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid grid-2">
        <div>
          {p && <SearchSettings p={p} onSaved={profile.refresh} />}
          {p && <MatchingSettings p={p} onSaved={profile.refresh} />}
        </div>
        <div>
          <div className="card">
            <h2>Resumes</h2>
            <div className="table-wrap" style={{ marginBottom: 12 }}>
              <table>
                <thead><tr><th>Id</th><th>File</th><th>Tags</th><th /></tr></thead>
                <tbody>
                  {resumes.data?.map((r) => (
                    <tr key={r.id}>
                      <td>{r.id}{r.default && <span className="badge info" style={{ marginLeft: 6 }}>default</span>}</td>
                      <td className="small">{r.path}</td>
                      <td className="small">{r.tags.join(", ")}</td>
                      <td>{r.exists ? <span className="badge ok">found</span> : <span className="badge err">missing</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <ResumeUpload onDone={() => { resumes.refresh(); profile.refresh(); }} />
          </div>
          <div className="card">
            <h2>Local AI (Ollama)</h2>
            {ai.data ? (
              <p className="small">
                {ai.data.available && ai.data.model_present
                  ? <><span className="badge ok">ready</span> model <code>{ai.data.model}</code></>
                  : <><span className="badge warn">unavailable</span> {ai.data.error} - matching and answers fall back to rules.</>}
              </p>
            ) : <p className="muted">Checking…</p>}
          </div>
          <SavedAnswers items={memory.data ?? []} onChange={memory.refresh} />
        </div>
      </div>
    </>
  );
}

function SearchSettings({ p, onSaved }: { p: Record<string, any>; onSaved: () => void }) {
  const s = p.search;
  const [queries, setQueries] = useState("");
  const [locations, setLocations] = useState("");
  const [portals, setPortals] = useState<string[]>([]);
  const [nums, setNums] = useState<Record<string, number>>({});
  const [easyOnly, setEasyOnly] = useState(false);
  const { busy, error, run } = useAction();
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setQueries(s.queries.join(", "));
    setLocations(s.locations.join(", "));
    setPortals(s.portals);
    setEasyOnly(s.linkedin_easy_apply_only);
    setNums({
      max_pages_per_query: s.max_pages_per_query, max_jobs_per_query: s.max_jobs_per_query,
      max_applications_per_run: s.max_applications_per_run, max_applications_per_day: s.max_applications_per_day,
      posted_within_days: s.posted_within_days,
    });
  }, [s]);

  async function save(e: FormEvent) {
    e.preventDefault();
    setSaved(false);
    const ok = await run(() => api.updateSettings({ search: { queries: list(queries), locations: list(locations), portals, linkedin_easy_apply_only: easyOnly, ...nums } }));
    if (ok) { setSaved(true); onSaved(); }
  }
  const labels: Record<string, string> = {
    max_pages_per_query: "Pages per search", max_jobs_per_query: "Jobs per search",
    max_applications_per_run: "Max applications per run", max_applications_per_day: "Max applications per day",
    posted_within_days: "Posted within (days)",
  };
  return (
    <form className="card" onSubmit={save}>
      <h2>Search</h2>
      <div className="form-grid">
        <label className="field" style={{ gridColumn: "1 / -1" }}>Roles to search (comma separated)
          <input type="text" value={queries} onChange={(e) => setQueries(e.target.value)} />
        </label>
        <label className="field" style={{ gridColumn: "1 / -1" }}>Locations (comma separated)
          <input type="text" value={locations} onChange={(e) => setLocations(e.target.value)} />
        </label>
        {Object.keys(labels).map((k) => (
          <label key={k} className="field">{labels[k]}
            <input type="number" min={0} value={nums[k] ?? 0} onChange={(e) => setNums({ ...nums, [k]: Number(e.target.value) })} />
          </label>
        ))}
      </div>
      <div className="row" style={{ margin: "12px 0" }}>
        <span className="muted small">Default portals:</span>
        {Object.entries(PORTAL_LABEL).map(([k, v]) => (
          <label key={k} className="check small">
            <input type="checkbox" checked={portals.includes(k)} onChange={() => setPortals(portals.includes(k) ? portals.filter((x) => x !== k) : [...portals, k])} />{v}
          </label>
        ))}
        <label className="check small"><input type="checkbox" checked={easyOnly} onChange={(e) => setEasyOnly(e.target.checked)} /> LinkedIn: Easy Apply only</label>
      </div>
      <div className="row">
        <button className="btn primary" disabled={busy}>Save search settings</button>
        {saved && <span className="small" style={{ color: "var(--ok)" }}>Saved</span>}
        {error && <span className="small lvl-error">{error}</span>}
      </div>
    </form>
  );
}

function MatchingSettings({ p, onSaved }: { p: Record<string, any>; onSaved: () => void }) {
  const [m, setM] = useState<Record<string, any>>({});
  const [minFe, setMinFe] = useState(0.5);
  const [targets, setTargets] = useState("");
  const [excluded, setExcluded] = useState("");
  const { busy, error, run } = useAction();
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setM(p.matching);
    setMinFe(p.job_preferences.min_frontend_ratio);
    setTargets(p.job_preferences.target_titles.join(", "));
    setExcluded(p.job_preferences.excluded_title_keywords.join(", "));
  }, [p]);

  async function save(e: FormEvent) {
    e.preventDefault();
    setSaved(false);
    const ok = await run(() => api.updateSettings({
      matching: m,
      job_preferences: { min_frontend_ratio: minFe, target_titles: list(targets), excluded_title_keywords: list(excluded) },
    }));
    if (ok) { setSaved(true); onSaved(); }
  }
  return (
    <form className="card" onSubmit={save}>
      <h2>Matching</h2>
      <div className="form-grid">
        <label className="field">Apply when score ≥
          <input type="number" min={0} max={100} value={m.apply_threshold ?? 70} onChange={(e) => setM({ ...m, apply_threshold: Number(e.target.value) })} />
        </label>
        <label className="field">Skip when score &lt;
          <input type="number" min={0} max={100} value={m.skip_threshold ?? 45} onChange={(e) => setM({ ...m, skip_threshold: Number(e.target.value) })} />
        </label>
        <label className="field">Min. frontend share (0-1)
          <input type="number" min={0} max={1} step={0.05} value={minFe} onChange={(e) => setMinFe(Number(e.target.value))} />
        </label>
        <label className="field">Experience tolerance (years)
          <input type="number" min={0} max={10} step={0.5} value={m.experience_tolerance_years ?? 2} onChange={(e) => setM({ ...m, experience_tolerance_years: Number(e.target.value) })} />
        </label>
        <label className="field" style={{ gridColumn: "1 / -1" }}>Target titles
          <textarea value={targets} onChange={(e) => setTargets(e.target.value)} />
        </label>
        <label className="field" style={{ gridColumn: "1 / -1" }}>Never apply when the title contains
          <textarea value={excluded} onChange={(e) => setExcluded(e.target.value)} />
        </label>
      </div>
      <div className="row" style={{ margin: "12px 0" }}>
        <label className="check small"><input type="checkbox" checked={!!m.use_ai} onChange={(e) => setM({ ...m, use_ai: e.target.checked })} /> Use local AI for ambiguous jobs</label>
        <label className="check small"><input type="checkbox" checked={!!m.location_strict} onChange={(e) => setM({ ...m, location_strict: e.target.checked })} /> Skip jobs outside my locations</label>
      </div>
      <div className="row">
        <button className="btn primary" disabled={busy}>Save matching settings</button>
        {saved && <span className="small" style={{ color: "var(--ok)" }}>Saved</span>}
        {error && <span className="small lvl-error">{error}</span>}
      </div>
    </form>
  );
}

function ResumeUpload({ onDone }: { onDone: () => void }) {
  const { busy, error, run } = useAction();
  const [msg, setMsg] = useState("");
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const ok = await run(() => api.uploadResume(form));
    if (ok) { setMsg("Uploaded"); e.currentTarget?.reset(); onDone(); }
  }
  return (
    <form onSubmit={submit} className="form-grid">
      <label className="field">File (PDF/DOC/DOCX)<input type="file" name="file" accept=".pdf,.doc,.docx" required /></label>
      <label className="field">Id<input type="text" name="resume_id" placeholder="frontend" required pattern="[A-Za-z0-9_-]{1,64}" /></label>
      <label className="field">Tags (comma separated)<input type="text" name="tags" placeholder="frontend, react" /></label>
      <label className="check small"><input type="checkbox" name="make_default" value="true" /> Make default</label>
      <div className="row">
        <button className="btn" disabled={busy}>Upload resume</button>
        {msg && <span className="small" style={{ color: "var(--ok)" }}>{msg}</span>}
        {error && <span className="small lvl-error">{error}</span>}
      </div>
    </form>
  );
}
