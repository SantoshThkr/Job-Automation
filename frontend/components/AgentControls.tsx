"use client";

import { useEffect, useState } from "react";
import { AgentState, Mode, Portal, api } from "@/lib/api";
import { useAction } from "@/lib/hooks";
import { MODE_LABEL, PORTAL_LABEL, timeAgo } from "@/lib/format";

const MODES: { value: Mode; hint: string }[] = [
  { value: "dry_run", hint: "Find, score and fill forms - never submits." },
  { value: "review", hint: "Fills everything, then waits for your Approve before each submit." },
  { value: "auto", hint: "Submits automatically when every answer is confident; otherwise flags it for you." },
];
const PORTALS: Portal[] = ["naukri", "linkedin", "instahyre"];

function stateTone(a: AgentState): string {
  if (!a.alive) return a.actual_state === "ERROR" ? "err" : "";
  if (a.actual_state.startsWith("WAITING") || a.actual_state === "PAUSED") return "wait";
  return "on";
}

export function AgentControls({ agent, onChange, defaultPortals }: {
  agent: AgentState | null;
  onChange: () => void;
  defaultPortals: Portal[];
}) {
  const { busy, error, run } = useAction();
  const [mode, setMode] = useState<Mode>("dry_run");
  const [portals, setPortals] = useState<Portal[]>(defaultPortals);
  const [touched, setTouched] = useState(false);

  useEffect(() => {
    if (!agent || touched) return;
    setMode(agent.mode);
    if (agent.portals?.length) setPortals(agent.portals);
    else if (defaultPortals.length) setPortals(defaultPortals);
  }, [agent, defaultPortals, touched]);

  const alive = !!agent?.alive;
  const desired = agent?.desired_state ?? "STOPPED";
  const running = alive && desired === "RUNNING";
  const paused = alive && desired === "PAUSED";

  async function chooseMode(m: Mode) {
    if (m === "auto" && !window.confirm("Auto Apply submits applications without asking you first (only when every answer is confident). Continue?")) return;
    setTouched(true);
    setMode(m);
    if (alive) await run(() => api.setMode(m));
    onChange();
  }

  function togglePortal(p: Portal) {
    setTouched(true);
    setPortals((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p]));
  }

  const act = (fn: () => Promise<unknown>) => async () => {
    await run(fn);
    onChange();
  };

  return (
    <div className="card">
      <div className="spread" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>Agent</h2>
        {agent && (
          <span className="row small">
            <span className={`dot ${stateTone(agent)}`} />
            <strong>{alive ? agent.actual_state.replaceAll("_", " ") : agent.actual_state === "ERROR" ? "ERROR" : "STOPPED"}</strong>
            {alive && <span className="muted">· heartbeat {timeAgo(agent.heartbeat_at)}</span>}
          </span>
        )}
      </div>

      <div style={{ marginBottom: 12 }}>
        <div className="segmented" role="radiogroup" aria-label="Mode">
          {MODES.map((m) => (
            <button key={m.value} className={mode === m.value ? "on" : ""} onClick={() => chooseMode(m.value)}
                    disabled={busy} role="radio" aria-checked={mode === m.value}>
              {MODE_LABEL[m.value]}
            </button>
          ))}
        </div>
        <p className="muted small" style={{ margin: "6px 0 0" }}>{MODES.find((m) => m.value === mode)?.hint}</p>
      </div>

      <div className="row" style={{ marginBottom: 14 }}>
        <span className="muted small">Portals:</span>
        {PORTALS.map((p) => (
          <label key={p} className="check small">
            <input type="checkbox" checked={portals.includes(p)} onChange={() => togglePortal(p)} disabled={alive} />
            {PORTAL_LABEL[p]}
          </label>
        ))}
      </div>

      <div className="row">
        <button className="btn primary" disabled={busy || running || paused || portals.length === 0}
                onClick={act(() => api.start(mode, portals))}>Start</button>
        <button className="btn" disabled={busy || !running} onClick={act(api.pause)}>Pause</button>
        <button className="btn" disabled={busy || !paused} onClick={act(api.resume)}>Resume</button>
        <button className="btn danger" disabled={busy || (!alive && desired === "STOPPED")} onClick={act(api.stop)}>Stop</button>
      </div>

      {agent && (agent.current_activity || agent.message) && (
        <p className="small" style={{ marginBottom: 0 }}>
          {agent.current_activity && <><strong>Now:</strong> {agent.current_activity}<br /></>}
          {agent.message && <span className="muted">{agent.message}</span>}
        </p>
      )}
      {agent?.last_error && <p className="small lvl-error" style={{ marginBottom: 0 }}>Last error: {agent.last_error}</p>}
      {error && <p className="small lvl-error" style={{ marginBottom: 0 }}>{error}</p>}
    </div>
  );
}
