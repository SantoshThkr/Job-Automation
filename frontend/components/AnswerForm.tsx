"use client";

import { useState } from "react";
import { QA, api } from "@/lib/api";
import { useAction } from "@/lib/hooks";

/** Answer a question the assistant could not answer; the answer is remembered for future forms. */
export function AnswerForm({ qa, onSaved }: { qa: QA; onSaved: () => void }) {
  const [value, setValue] = useState(qa.answer ?? "");
  const [saved, setSaved] = useState(false);
  const { busy, error, run } = useAction();
  if (saved) return <div className="small" style={{ color: "var(--ok)" }}>Saved - used from now on</div>;
  return (
    <form className="row" style={{ marginTop: 6 }} onSubmit={async (e) => {
      e.preventDefault();
      const ok = await run(() => api.saveAnswer(qa.question, value));
      if (ok) { setSaved(true); onSaved(); }
    }}>
      {qa.options.length ? (
        <select value={value} onChange={(e) => setValue(e.target.value)} required>
          <option value="">Choose…</option>
          {qa.options.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      ) : (
        <input type="text" value={value} onChange={(e) => setValue(e.target.value)} placeholder="Your answer" required />
      )}
      <button className="btn small" disabled={busy || !value.trim()}>Save answer</button>
      {error && <span className="small lvl-error">{error}</span>}
    </form>
  );
}
