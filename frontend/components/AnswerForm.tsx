"use client";

import { useState } from "react";
import { QA, api } from "@/lib/api";
import { uniqueStrings } from "@/lib/format";
import { useAction } from "@/lib/hooks";

/** Answer a question the assistant could not answer; the answer is remembered for future forms. */
export function AnswerForm({ qa, onSaved }: { qa: QA; onSaved: () => void }) {
  const [value, setValue] = useState(qa.answer ?? "");
  const [saved, setSaved] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  const choices = uniqueStrings(qa.options);
  if (saved) return <div className="small" style={{ color: "var(--ok)" }}>{saved}</div>;
  return (
    <form className="row" style={{ marginTop: 6 }} onSubmit={async (e) => {
      e.preventDefault();
      const res = await run(() => api.saveAnswer(qa.question, value, qa.job_id));
      if (res) {
        const n = res.requeued_jobs?.length ?? 0;
        setSaved(n ? `Saved - ${n} job(s) queued again automatically` : "Saved - used from now on");
        onSaved();
      }
    }}>
      {choices.length ? (
        <select value={value} onChange={(e) => setValue(e.target.value)} required>
          <option value="">Choose…</option>
          {choices.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      ) : (
        <input type="text" value={value} onChange={(e) => setValue(e.target.value)} placeholder="Your answer" required />
      )}
      <button className="btn small" disabled={busy || !value.trim()}>Save answer</button>
      {error && <span className="small lvl-error">{error}</span>}
    </form>
  );
}
