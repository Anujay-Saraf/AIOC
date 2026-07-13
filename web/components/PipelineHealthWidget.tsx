"use client";

import { useEffect, useState } from "react";
import { Play, Zap, X, Check } from "lucide-react";
import { getPipelinesHealth, simulatePipelines } from "@/lib/api";
import { pipelineSimulatorStatus, controlPipelineSimulator } from "@/lib/api";

export function PipelineHealthWidget() {
  const [health, setHealth] = useState<Record<string, any> | null>(null);
  const [running, setRunning] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      setError(null);
      setNotFound(false);
      setHealth(await getPipelinesHealth());
    } catch (e) {
      const anyE: any = e;
      if (anyE && anyE.status === 404) {
        setNotFound(true);
        setHealth(null);
        setError(null);
        return;
      }
      setError(e instanceof Error ? e.message : "Failed to load pipeline health");
    }
  }

  useEffect(() => {
    refresh();
    let t = window.setInterval(refresh, 5000);

    // poll simulator state
    let s: number | null = null;
    async function pollStatus() {
      try {
        const st = await pipelineSimulatorStatus();
        setRunning(!!st.running);
      } catch (e) {}
    }
    pollStatus();
    s = window.setInterval(pollStatus, 6000);
    return () => window.clearInterval(t);
  }, []);

  async function triggerSim(p?: string) {
    setRunning(true);
    try {
      await simulatePipelines(p);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Simulation failed");
    } finally {
      setRunning(false);
    }
  }

  async function toggleSimulator() {
    try {
      setError(null);
      const action = running ? "stop" : "start";
      const res = await controlPipelineSimulator(action as "start" | "stop");
      setRunning(!!res.running);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to toggle simulator");
    }
  }

  return (
    <section className="content-section">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Pipeline Health</p>
          <h2>Demo ingestion pipeline health</h2>
        </div>
        <div className="button-row">
          <button className="ghost-button" onClick={() => refresh()} disabled={false}>Refresh</button>
          <button className="primary-button" onClick={() => triggerSim("all")} disabled={false}><Play size={14} /> Simulate fetch</button>
          <button className={`ghost-button ${running ? 'danger-button' : ''}`} onClick={() => toggleSimulator()}>
            {running ? <><X size={14} /> Stop simulator</> : <><Check size={14} /> Start simulator</>}
          </button>
        </div>
      </div>

      {error && <div className="error-panel">{error}</div>}
      {notFound && (
        <div className="empty-panel">
          <strong>404 — Pipeline health not available</strong>
          <p>The simulation health endpoint returned 404. The demo pipelines may be disabled.</p>
          <div className="button-row">
            <button className="ghost-button" onClick={() => refresh()}>Retry</button>
          </div>
        </div>
      )}

      <div className="pipeline-grid">
        {health ? Object.values(health).map((p: any) => (
          <article key={p.name} className={`pipeline-card ${p.status === 'ok' ? 'pipeline-ok' : p.status === 'failed' ? 'pipeline-failed' : ''}`}>
            <div className="pipeline-card-head">
              <strong style={{ textTransform: 'capitalize' }}>{p.name}</strong>
              <span className="status-pill" style={{ background: p.status === 'ok' ? 'rgba(34,197,94,.12)' : p.status === 'failed' ? 'rgba(239,68,68,.12)' : '#334155' }}>
                {p.status}
              </span>
            </div>
            <div className="pipeline-card-body">
              <div><small>Last checked</small><div>{p.last_checked || '-'}</div></div>
              <div><small>Latency (ms)</small><div>{p.latency_ms ?? '-'}</div></div>
              <div><small>Error rate</small><div>{p.error_rate ?? '-'}</div></div>
            </div>
            <div className="pipeline-card-actions">
              <button className="ghost-button" onClick={() => triggerSim(p.name)} disabled={running}><Zap size={14} /> Check</button>
            </div>
          </article>
        )) : <div>Loading...</div>}
      </div>

      <style jsx>{`
        .pipeline-grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); }
        .pipeline-card { padding: 12px; border-radius: 12px; border: 1px solid var(--line); background: var(--panel-soft); }
        .pipeline-card-head { display:flex; justify-content:space-between; align-items:center; }
        .pipeline-card-body { display:grid; grid-template-columns: 1fr 1fr 1fr; gap:8px; margin-top:8px; }
        .pipeline-card-actions { margin-top:10px; display:flex; gap:8px; }
      `}</style>
    </section>
  );
}
