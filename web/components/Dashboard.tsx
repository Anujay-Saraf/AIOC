"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock3,
  History,
  Play,
  RotateCcw,
  ShieldCheck,
  Sparkles
} from "lucide-react";
import { listConnectors, listIncidents, resetIncidents, triggerIncident, uploadIncidentLog, uploadKnowledge } from "@/lib/api";
import type { Connector, Incident } from "@/lib/types";
import { IncidentAnalyticsPanel } from "@/components/IncidentAnalyticsPanel";

const scenarios = [
  {
    label: "DB Pool Exhaustion",
    service: "payment-api",
    severity: "critical",
    timestamp: "2026-07-07T14:32:15Z",
    alert_description: "Database connection pool exhaustion detected"
  },
  {
    label: "Memory Leak",
    service: "order-processor",
    severity: "critical",
    timestamp: "2026-07-07T16:30:00Z",
    alert_description: "Memory leak detected - GC pause times increasing"
  },
  {
    label: "Cascading Timeout",
    service: "checkout-gateway",
    severity: "critical",
    timestamp: "2026-07-07T17:05:05Z",
    alert_description: "Cascading failure - downstream service timeout"
  },
  {
    label: "Cache Stampede",
    service: "search-api",
    severity: "critical",
    timestamp: "2026-07-07T19:09:00Z",
    alert_description: "Latency spike from cache stampede and retry amplification"
  },
  {
    label: "High Impact Checkout",
    service: "checkout-gateway",
    severity: "critical",
    timestamp: "2026-07-11T09:15:00Z",
    alert_description: "Real-time high-impact checkout authorization failures affecting project revenue",
    high_impact: true,
    alert_tier: "high_impact"
  }
];

type IncidentGroup = {
  signature: string;
  latest: Incident;
  incidents: Incident[];
  actionableCount: number;
};

export function Dashboard() {
  const router = useRouter();
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [loading, setLoading] = useState(true);
  const [triggering, setTriggering] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploadStatus, setUploadStatus] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadMode, setUploadMode] = useState<"knowledge" | "incident">("knowledge");
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [sourceConnectorId, setSourceConnectorId] = useState<string>("");
  const [showHistory, setShowHistory] = useState(false);

  const sortedIncidents = useMemo(() => [...incidents].sort(compareIncidentTime), [incidents]);
  const groups = useMemo(() => groupIncidents(sortedIncidents), [sortedIncidents]);
  const actionGroups = useMemo(
    () => groups.filter((group) => group.actionableCount > 0),
    [groups]
  );
  const historyGroups = useMemo(
    () => groups.filter((group) => group.actionableCount === 0),
    [groups]
  );
  const investigating = useMemo(
    () => incidents.filter((incident) => incident.current_status !== "complete").length,
    [incidents]
  );
  const needsReview = actionGroups.filter((group) => group.latest.current_status === "complete").length;

  async function refresh() {
    try {
      setError(null);
      setIncidents(await listIncidents());
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to load incidents");
    } finally {
      setLoading(false);
    }
  }

  async function refreshConnectors() {
    try {
      setConnectors((await listConnectors()).connectors);
    } catch (exc) {
      // ignore connector load failure; upload can still work without configured connectors
    }
  }

  useEffect(() => {
    refresh();
    refreshConnectors();
    const timer = window.setInterval(refresh, 1500);
    return () => window.clearInterval(timer);
  }, []);

  async function launch(index: number) {
    const scenario = scenarios[index];
    setTriggering(scenario.label);
    try {
      setError(null);
      const incident = await triggerIncident(scenario);
      setIncidents((items) => [incident, ...items]);
      router.push(`/incident/${incident.incident_id}`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to launch incident");
    } finally {
      setTriggering(null);
    }
  }

  async function clearDemoQueue() {
    if (!window.confirm("Clear all in-memory demo incidents and start fresh?")) return;
    try {
      setError(null);
      await resetIncidents();
      setIncidents([]);
      setShowHistory(false);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to reset demo queue");
    }
  }

  const sourceConnectors = useMemo(
    () => connectors.filter((connector) => ["aws_s3", "gcp_bucket", "azure_blob", "log_stream"].includes(connector.type)),
    [connectors]
  );

  async function uploadFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setError(null);
    setUploadStatus(null);
    setUploading(true);
    try {
      if (uploadMode === "incident") {
        const result = await uploadIncidentLog(file, undefined, sourceConnectorId || undefined);
        setUploadStatus(
          `Uploaded ${file.name}. Incident analysis created for ${result.incident?.service || "uploaded logs"}.`
        );
        if (result.incident?.incident_id) {
          await refresh();
          router.push(`/incident/${result.incident.incident_id}`);
          return;
        }
      } else {
        const result = await uploadKnowledge(file);
        setUploadStatus(`Uploaded ${result.title || file.name}. Searchable knowledge is now available.`);
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to upload file");
    } finally {
      setUploading(false);
      event.target.value = "";
    }
  }

  return (
    <div className="screen-stack">
      <section className="hero-panel">
        <div>
          <p className="eyebrow">AI Incident Response</p>
          <h1>Command every incident from signal to review.</h1>
          <p className="hero-copy">
            Launch investigations, inspect grounded RCA, approve recovery, and keep a traceable human review trail from phone or desktop.
          </p>
        </div>
        <div className="hero-metrics" aria-label="Incident overview">
          <Metric label="Investigating" value={investigating} />
          <Metric label="Needs Review" value={needsReview} />
          <Metric label="Total" value={incidents.length} />
        </div>
      </section>

      <section className="launch-grid" aria-label="Scenarios">
        {scenarios.map((scenario, index) => (
          <article className="scenario-tile" key={scenario.label} aria-busy={triggering === scenario.label}>
            <div className="scenario-tile-copy">
              <span className="tile-icon"><Play size={18} /></span>
              <span className="tile-text">
                <strong>{scenario.label}</strong>
                <small>{scenario.service} | {scenario.alert_description}</small>
              </span>
            </div>
            <button
              className="scenario-launch"
              onClick={() => launch(index)}
              disabled={Boolean(triggering)}
            >
              {triggering === scenario.label ? "Launching..." : "Open scenario"}
            </button>
          </article>
        ))}
      </section>

      <section className="content-section upload-section">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Ingest Logs & Docs</p>
            <h2>Upload incident evidence</h2>
          </div>
          <span className="panel-note">Uploaded files are indexed and available for knowledge search and incident QA.</span>
        </div>
        <div className="upload-panel">
          <div className="upload-options-row">
            <label className="upload-select-label">
              <span>Upload mode</span>
              <select value={uploadMode} onChange={(event) => setUploadMode(event.target.value as "knowledge" | "incident")} disabled={uploading}>
                <option value="knowledge">Index as knowledge</option>
                <option value="incident">Analyze as incident log</option>
              </select>
            </label>
            <label className="upload-select-label">
              <span>Cloud/infra source</span>
              <select
                value={sourceConnectorId}
                onChange={(event) => setSourceConnectorId(event.target.value)}
                disabled={uploading || sourceConnectors.length === 0}
              >
                <option value="">Direct upload</option>
                {sourceConnectors.map((connector) => (
                  <option key={connector.id} value={connector.id}>
                    {connector.name} ({connector.type})
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="upload-button">
            <input type="file" accept=".txt,.md,.log,.json,.yaml,.yml" onChange={uploadFile} disabled={uploading} />
            {uploading ? "Uploading…" : uploadMode === "incident" ? "Upload log for incident analysis" : "Select a log or knowledge file"}
          </label>
          <p>
            {uploadMode === "incident"
              ? "Upload a log file to create an incident analysis record."
              : "Drop a log, runbook, or incident note to strengthen the system's retrieval and analysis context."}
          </p>
          {uploadStatus && <div className="success-panel">{uploadStatus}</div>}
        </div>
      </section>

      <IncidentAnalyticsPanel />

      {error && <div className="error-panel">{error}</div>}

      <section id="incidents" className="content-section">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Action Queue</p>
            <h2>Incidents that need attention</h2>
          </div>
          <div className="button-row">
            <button className="ghost-button" onClick={refresh}>Refresh</button>
            {incidents.length > 0 && (
              <button className="danger-button" onClick={clearDemoQueue}>
                <RotateCcw size={16} /> Clear Demo
              </button>
            )}
          </div>
        </div>

        {loading ? (
          <div className="empty-panel">Loading incidents...</div>
        ) : actionGroups.length === 0 ? (
          <div className="empty-panel">
            <Sparkles size={24} />
            <strong>No open action items.</strong>
            <span>Launch a scenario to start an investigation, or open history for completed runs.</span>
          </div>
        ) : (
          <div className="incident-list">
            {actionGroups.map((group) => (
              <IncidentCard key={group.signature} group={group} />
            ))}
          </div>
        )}
      </section>

      {historyGroups.length > 0 && (
        <section className="content-section muted-section">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Run History</p>
              <h2>{historyGroups.length} completed incident pattern{historyGroups.length === 1 ? "" : "s"}</h2>
            </div>
            <button className="ghost-button" onClick={() => setShowHistory((value) => !value)}>
              <History size={16} /> {showHistory ? "Hide" : "Show"} History
            </button>
          </div>
          {showHistory ? (
            <div className="incident-list compact-list">
              {historyGroups.map((group) => (
                <IncidentCard key={group.signature} group={group} compact />
              ))}
            </div>
          ) : (
            <div className="archive-summary">
              Historical duplicate runs are archived here so the command queue stays readable.
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function IncidentCard({ group, compact = false }: { group: IncidentGroup; compact?: boolean }) {
  const incident = group.latest;
  const confidence = Math.round((incident.root_cause?.confidence || incident.rca_confidence || 0) * 100);
  const complete = incident.current_status === "complete";
  const statusLabel = getStatusLabel(incident);
  const hiddenRuns = group.incidents.length - 1;

  return (
    <Link className={`incident-card ${compact ? "compact-card" : ""} ${isActionable(incident) ? "actionable-card" : ""}`} href={`/incident/${incident.incident_id}`}>
      <div className="incident-card-top">
        <div>
          <div className="pill-row">
            <span className={`severity-pill severity-${incident.severity || "unknown"}`}>{incident.severity || "unknown"}</span>
            <span className="status-pill">{statusLabel}</span>
            {hiddenRuns > 0 && <span className="hidden-count">{hiddenRuns} older run{hiddenRuns === 1 ? "" : "s"} hidden</span>}
          </div>
          <h3>{incident.service}</h3>
          <p>{incident.alert_description}</p>
        </div>
        <span className="open-arrow"><ArrowRight size={20} /></span>
      </div>
      {!compact && (
        <div className="incident-progress">
          <span style={{ width: `${confidence}%` }} />
        </div>
      )}
      <div className="incident-card-bottom">
        <span><Clock3 size={15} /> {incident.current_status}</span>
        <span>{confidence}% confidence</span>
        <span>{complete ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />} {incident.lifecycle_status}</span>
        <span><ShieldCheck size={15} /> {incident.quality_gates?.overall_passed ? "gates pass" : "reviewing"}</span>
      </div>
      {!compact && (
        <div className="card-meta">
          <span>{group.incidents.length} total run{group.incidents.length === 1 ? "" : "s"} for this signal</span>
          <strong>Open investigation</strong>
        </div>
      )}
    </Link>
  );
}

function groupIncidents(items: Incident[]): IncidentGroup[] {
  const groups = new Map<string, IncidentGroup>();
  for (const incident of items) {
    const signature = [
      incident.service || "unknown-service",
      incident.alert_description || "unknown-alert"
    ].join("::");
    const existing = groups.get(signature);
    if (!existing) {
      groups.set(signature, {
        signature,
        latest: incident,
        incidents: [incident],
        actionableCount: isActionable(incident) ? 1 : 0
      });
      continue;
    }
    existing.incidents.push(incident);
    if (compareIncidentTime(incident, existing.latest) < 0) {
      existing.latest = incident;
    }
    if (isActionable(incident)) {
      existing.actionableCount += 1;
    }
  }
  return [...groups.values()].sort((left, right) => compareIncidentTime(left.latest, right.latest));
}

function isActionable(incident: Incident) {
  if (incident.current_status !== "complete") return true;
  const lifecycle = (incident.lifecycle_status || "").toLowerCase();
  if (
    [
      "reviewing",
      "investigating",
      "needs_review",
      "needs_human_review",
      "review_requested_more_data"
    ].includes(lifecycle)
  ) return true;
  if (incident.quality_gates?.overall_passed === false) return true;
  return false;
}

function getStatusLabel(incident: Incident) {
  if (incident.current_status !== "complete") return "Agents running";
  if (isActionable(incident)) return "Human review needed";
  return "Reviewed";
}

function compareIncidentTime(left: Incident, right: Incident) {
  return timestampOf(right) - timestampOf(left);
}

function timestampOf(incident: Incident) {
  return Date.parse(incident.created_at || incident.timestamp || "") || 0;
}
