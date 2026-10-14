"use client";

import { useEffect, useState } from "react";
import { Recency, RecencyChoice, api } from "@/lib/api";
import { useAction } from "@/lib/hooks";
import { localDay, recencyOption } from "@/lib/format";

const DEFAULT_CHOICES = [1, 3, 7, 14, 30, 60, 90];

/** The select's value for a stored window: "7", "custom" or "all". */
export function recencyValue(r: Recency | null | undefined): string {
  if (!r) return "";
  if (r.all) return "all";
  if (r.start || r.end) return "custom";
  return String(r.days ?? 7);
}

/** Why a custom range cannot be used, or "" - the same rules the backend enforces. */
export function rangeProblem(start: string, end: string, today = localDay()): string {
  if (!start) return "Choose the first day of the range.";
  if (end && start > end) return "The start date is after the end date.";
  if ((end || today) > today) return "The range cannot end in the future.";
  if (start < localDay(-366, today)) return "The range must lie within the last year.";
  return "";
}

/**
 * The job-recency window: which posting dates the assistant searches, queues and applies to. Saved in the backend,
 * so it survives refreshes and applies to every run until changed. "All dates" is only ever chosen explicitly.
 */
export function RecencyPicker({ recency, onChange }: { recency: Recency | null | undefined; onChange: () => void }) {
  const { busy, error, run, setError } = useAction();
  const stored = recencyValue(recency);
  const [choice, setChoice] = useState(stored);
  const [start, setStart] = useState(recency?.start ?? "");
  const [end, setEnd] = useState(recency?.end ?? "");
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (editing) return;
    setChoice(stored);
    setStart(recency?.start ?? "");
    setEnd(recency?.end ?? "");
  }, [stored, recency?.start, recency?.end, editing]);

  async function save(body: RecencyChoice) {
    const saved = await run(() => api.setRecency(body));
    if (saved) {
      setEditing(false);
      onChange();
    }
  }

  async function choose(value: string) {
    setError(null);
    if (value === "custom") {
      setEditing(true);
      setChoice("custom");
      return;
    }
    if (value === "all" && !window.confirm(
      "All dates: the assistant may search, queue and apply to jobs posted weeks or months ago. Continue?")) {
      return;
    }
    setChoice(value);
    await save(value === "all" ? { all: true } : { days: Number(value) });
  }

  const problem = choice === "custom" ? rangeProblem(start, end) : "";
  const choices = recency?.choices?.length ? recency.choices : DEFAULT_CHOICES;

  return (
    <div className="recency">
      <div className="row small">
        <label className="muted" htmlFor="job-recency">Job recency:</label>
        <select id="job-recency" aria-label="Job recency" value={choice} disabled={busy || !recency}
                onChange={(e) => choose(e.target.value)}>
          {!recency && <option value="">…</option>}
          {choices.map((d) => <option key={d} value={String(d)}>{recencyOption(d)}</option>)}
          <option value="custom">Custom range…</option>
          <option value="all">All dates</option>
        </select>
        {choice === "custom" && (
          <>
            <input type="date" aria-label="Posted from" value={start} max={end || localDay()} min={localDay(-366)}
                   onChange={(e) => { setEditing(true); setStart(e.target.value); }} />
            <span className="muted">to</span>
            <input type="date" aria-label="Posted until" value={end} min={start || undefined} max={localDay()}
                   onChange={(e) => { setEditing(true); setEnd(e.target.value); }} />
            <button className="btn small" disabled={busy || !!problem || (!editing && stored === "custom")}
                    onClick={() => save({ start, end: end || null })}>Apply range</button>
            {editing && stored !== "custom" && (
              <button className="btn small" disabled={busy} onClick={() => { setEditing(false); setChoice(stored); }}>
                Cancel
              </button>
            )}
          </>
        )}
      </div>
      {recency && (
        <p className="muted small" data-testid="recency-label" style={{ margin: "4px 0 0" }}>
          {recency.all
            ? <>Posting dates are <strong>not limited</strong> - old jobs can be applied to.</>
            : <>Applying only to jobs <strong>{recency.label}</strong>{recency.chosen ? "" : " (the default)"}. Search,
              the queue and the last check before every application use it; a job whose posting date is unknown is
              never applied to automatically.</>}
        </p>
      )}
      {choice === "custom" && editing && problem && <p className="small lvl-warning" style={{ margin: "4px 0 0" }}>{problem}</p>}
      {error && <p className="small lvl-error" style={{ margin: "4px 0 0" }}>{error}</p>}
    </div>
  );
}
