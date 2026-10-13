"use client";

import { useState } from "react";
import { api, MemoryItem } from "@/lib/api";
import { useAction } from "@/lib/hooks";
import { PORTAL_LABEL } from "@/lib/format";

/** Where a saved answer is reused, in words. */
export function scopeLabel(m: MemoryItem): string {
  switch (m.scope) {
    case "job": return `only for the job "${m.job}"${m.company ? ` at ${m.company}` : ""}`;
    case "company": return m.company ? `only for ${m.company}` : "only for one company";
    case "portal": return `only on ${PORTAL_LABEL[m.portal ?? ""] ?? m.portal ?? "one portal"}`;
    case "category": return "every wording of this question";
    default: return "everywhere";
  }
}

/** Your saved answers: what is reused where, with a way to correct or remove each one. */
export function SavedAnswers({ items, onChange }: { items: MemoryItem[]; onChange: () => void }) {
  return (
    <div className="card">
      <h2>Saved answers</h2>
      <p className="muted small">
        Answers you gave for questions the assistant could not answer. Reused automatically - the most specific one
        that applies wins (this job, then this company, then this portal, then everywhere).
      </p>
      {!items.length && <p className="muted small">None yet.</p>}
      <ul className="timeline">
        {items.map((m) => <SavedAnswerRow key={m.id} m={m} onChange={onChange} />)}
      </ul>
    </div>
  );
}

function SavedAnswerRow({ m, onChange }: { m: MemoryItem; onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(m.answer);
  const { busy, error, run } = useAction();
  const save = () => run(async () => {
    await api.updateMemory(m.id, value.trim());
    setEditing(false);
    onChange();
  });
  return (
    <li style={{ gridTemplateColumns: "1fr auto" }}>
      <span className="small">
        <strong>{m.question}</strong><br />
        {editing ? (
          <textarea aria-label={`Corrected answer for: ${m.question}`} value={value} rows={2}
            onChange={(e) => setValue(e.target.value)} />
        ) : m.answer}
        {" "}<span className="muted">· used {m.times_used}× · reused {scopeLabel(m)}</span>
        {error && <span className="lvl-error"> · {error}</span>}
      </span>
      <span className="row">
        {editing ? (
          <>
            <button className="btn small" disabled={busy || !value.trim() || value.trim() === m.answer} onClick={save}>Save</button>
            <button className="btn small" disabled={busy} onClick={() => { setValue(m.answer); setEditing(false); }}>Cancel</button>
          </>
        ) : (
          <button className="btn small" onClick={() => setEditing(true)}>Edit</button>
        )}
        <button className="btn small danger" disabled={busy}
          onClick={() => run(async () => { await api.deleteMemory(m.id); onChange(); })}>Delete</button>
      </span>
    </li>
  );
}
