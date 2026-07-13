"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  BadgeCheck,
  Check,
  CircleAlert,
  Database,
  Download,
  FileJson,
  FileSearch,
  Gauge,
  RefreshCw,
  SearchCheck,
  ShieldCheck,
  Target,
  Users,
  X
} from "lucide-react";
import {
  ApiError,
  decideRemediation,
  getIncident,
  getIncidentJourney,
  getTrace,
  listIncidents,
  overrideRootCause,
  postmortemUrl,
  requestMoreData,
  reviewRca
} from "@/lib/api";
import type { Incident, IncidentJourneyResponse, TraceExport } from "@/lib/types";
import { CommandAssistant } from "@/components/CommandAssistant";

const tabs = ["overview", "flow", "evidence", "review", "trace"] as const;
type Tab = (typeof tabs)[number];

type FlowStepStatus = "complete" | "active" | "pending" | "blocked" | "review";

type FlowStep = {
  key: string;
  title: string;
  description: string;
  completedStep?: string;
  agents: string[];
  icon: "database" | "logs" | "metrics" | "rca" | "debate" | "impact" | "summary" | "review";
};

const flowSteps: FlowStep[] = [
  {
    key: "load_data",
    title: "Load Evidence",
    description: "Fetch logs, metrics, deployments, service context, and evidence IDs.",
    completedStep: "incident_commander",
    agents: ["incident_commander"],
    icon: "database"
  },
  {
    key: "log_analysis",
    title: "Analyze Logs",
    description: "Detect timeout, dependency, GC, memory, and runtime error patterns.",
    completedStep: "log_analysis",
    agents: ["log_analysis"],
    icon: "logs"
  },
  {
    key: "metrics_analysis",
    title: "Analyze Metrics",
    description: "Compare incident-window metrics against baseline behavior.",
    completedStep: "metrics_analysis",
    agents: ["metrics_analysis"],
    icon: "metrics"
  },
  {
    key: "deployment_analysis",
    title: "Deployment Analysis",
    description: "Correlate recent deployments with incident timeline and flag risky changes.",
    completedStep: "deployment_analysis",
    agents: ["deployment_analysis"],
    icon: "metrics"
  },
  {
    key: "rca_analysis",
    title: "Root Cause",
    description: "Generate hypothesis, confidence breakdown, and ruled-out alternatives.",
    completedStep: "rca_analysis",
    agents: ["rca_agent", "rca_analysis", "memory"],
    icon: "rca"
  },
  {
    key: "rca_debate",
    title: "Challenge & Judge",
    description: "Dynamic critics challenge evidence and safety before a judge accepts the RCA.",
    completedStep: "rca_debate",
    agents: ["evidence_critic", "operations_critic", "rca_reviser", "debate_judge"],
    icon: "debate"
  },
  {
    key: "business_impact",
    title: "Business Impact",
    description: "Estimate affected users and revenue impact from service metrics.",
    completedStep: "business_impact",
    agents: ["business_impact"],
    icon: "impact"
  },
  {
    key: "recovery_recommendations",
    title: "Recovery Plan",
    description: "Generate prioritised, risk-tagged recovery steps with approval flags.",
    completedStep: "recovery_recommendations",
    agents: ["recovery_recommendations"],
    icon: "summary"
  },
  {
    key: "summary",
    title: "Summaries",
    description: "Generate engineering notes, executive summary, and recovery plan.",
    completedStep: "summary",
    agents: ["executive_summary"],
    icon: "summary"
  },
  {
    key: "human_review",
    title: "Human Review",
    description: "Accept/reject RCA, request more data, or approve remediation actions.",
    agents: ["human_reviewer"],
    icon: "review"
  }
];

export function IncidentDetail({ incidentId }: { incidentId: string }) {
  const router = useRouter();
  const [incident, setIncident] = useState<Incident | null>(null);
  const [trace, setTrace] = useState<TraceExport | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [busy, setBusy] = useState(false);
  const [missing, setMissing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [journey, setJourney] = useState<IncidentJourneyResponse | null>(null);

  async function refresh() {
    try {
      const nextIncident = await getIncident(incidentId);
      setIncident(nextIncident);
      const nextJourney = await getIncidentJourney(incidentId).catch(() => null);
      setJourney(nextJourney);
      setMissing(false);
      setLoadError(null);
      if (tab === "trace") {
        setTrace(await getTrace(incidentId));
      }
    } catch (exc) {
      if (exc instanceof ApiError && exc.status === 404) {
        setMissing(true);
        setIncident(null);
        setTrace(null);
        setLoadError("This incident ID is not available in the currently running backend.");
        return;
      }
      setLoadError(exc instanceof Error ? exc.message : "Failed to load incident");
    }
  }

  useEffect(() => {
    if (missing) return;
    let timer: number | null = null;
    let es: EventSource | null = null;

    // Prefer server-sent events for real-time updates; fallback to polling.
    try {
      const streamUrl = `${process.env.NEXT_PUBLIC_API_BASE || ''}/api/incidents/${incidentId}/stream`;
      es = new EventSource(streamUrl);
      es.onmessage = (ev) => {
        try {
          const payload = JSON.parse(ev.data);
          setIncident(payload);
          // refresh journey/trace selectively when on relevant tabs
          if (tab === "flow") {
            getIncidentJourney(incidentId).then((j) => setJourney(j)).catch(() => null);
          }
          if (tab === "trace") {
            getTrace(incidentId).then((t) => setTrace(t)).catch(() => null);
          }
        } catch (e) {
          // ignore parse errors
        }
      };
      es.onerror = () => {
        // On error, close and fall back to polling
        try { es && es.close(); } catch {};
        es = null;
        refresh();
        timer = window.setInterval(refresh, 1500);
      };
    } catch (err) {
      refresh();
      timer = window.setInterval(refresh, 1500);
    }

    // Initial fetch to populate fast
    refresh();

    return () => {
      if (timer) window.clearInterval(timer);
      try { es && es.close(); } catch {}
    };
  }, [incidentId, tab, missing]);

  const confidence = Math.round((incident?.root_cause?.confidence || 0) * 100);
  const evidenceRefs = incident?.root_cause?.supporting_evidence_refs || [];
  const gates = incident?.quality_gates || {};
  const recommendations = incident?.recovery_recommendations || [];
  const decisions = incident?.remediation_decisions || {};
  const completedSteps = incident?.completed_steps || [];

  async function mutate(action: () => Promise<Incident>) {
    setBusy(true);
    try {
      setIncident(await action());
      if (tab === "trace") setTrace(await getTrace(incidentId));
    } finally {
      setBusy(false);
    }
  }

  const traceSpans = useMemo(() => trace?.spans || incident?.agent_invocations || [], [trace, incident]);
  const debateSpans = useMemo(
    () => traceSpans.filter((span) => Boolean(span.debate_role)),
    [traceSpans]
  );
  const flowState = useMemo(
    () => incident ? buildFlowState(incident, traceSpans, completedSteps) : null,
    [incident, traceSpans, completedSteps]
  );

  async function openLatestIncident() {
    const incidents = await listIncidents();
    const latest = incidents[0];
    router.replace(latest ? `/incident/${latest.incident_id}` : "/");
  }

  if (missing) {
    return (
      <MissingIncidentPanel
        incidentId={incidentId}
        message={loadError}
        openLatestIncident={openLatestIncident}
      />
    );
  }

  if (!incident || !flowState) {
    return (
      <div className="empty-panel">
        <strong>Loading incident...</strong>
        {loadError && <span>{loadError}</span>}
      </div>
    );
  }

  return (
    <div className="screen-stack">
      <section className="detail-hero">
        <Link className="back-button" href="/"><ArrowLeft size={18} /> Back</Link>
        <div className="detail-title-row">
          <div>
            <span className={`severity-pill severity-${incident.severity || "unknown"}`}>{incident.severity}</span>
            <h1>{incident.service}</h1>
            <p>{incident.alert_description}</p>
          </div>
          <a className="icon-action" href={postmortemUrl(incident.incident_id)} title="Download postmortem">
            <Download size={20} />
          </a>
        </div>
        <div className="hero-metrics compact">
          <Metric label="Confidence" value={`${confidence}%`} />
          <Metric label="Users" value={(incident.affected_users || 0).toLocaleString()} />
          <Metric label="Lifecycle" value={incident.lifecycle_status || "opened"} />
        </div>
      </section>

      <div className="tab-bar" role="tablist">
        {tabs.map((item) => (
          <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>
            {item}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <section className="content-section">
          <div className="rca-card">
            <div className="rca-card-header">
              <SearchCheck size={22} />
              <div>
                <p className="eyebrow">Root Cause</p>
                <h2>{incident.root_cause?.hypothesis || "Pending analysis"}</h2>
              </div>
            </div>
            <div className="confidence-track"><span style={{ width: `${confidence}%` }} /></div>
            {incident.root_cause?.deploy_correlation && <p className="callout">{incident.root_cause.deploy_correlation}</p>}
            <div className="summary-grid">
              <SummaryBlock title="Executive Summary" text={incident.executive_summary || "Pending"} />
              <SummaryBlock title="Engineering Notes" text={incident.engineering_summary || "Pending"} />
            </div>
          </div>
        </section>
      )}

      {tab === "flow" && (
        <section className="content-section">
          <div className="section-heading">
            <div>
              <p className="eyebrow">LangGraph Flow</p>
              <h2>Agent workflow map</h2>
            </div>
            <button className="ghost-button" onClick={refresh}><RefreshCw size={16} /> Refresh</button>
          </div>

          <div className="flow-summary-grid">
            <Metric label="Current" value={flowState.currentLabel} />
            <Metric label="Completed" value={`${flowState.completedCount}/${flowSteps.length}`} />
            <Metric label="Trace Spans" value={traceSpans.length} />
          </div>

          {journey?.resolution && (
            <div className="flow-resolution-panel">
              <div>
                <p className="eyebrow">Recommended Resolution</p>
                <h3>{journey.resolution.summary}</h3>
                <p>
                  Owner: {journey.resolution.owner || "service owner"} · Safety check: {journey.resolution.safety_check || "validate KPIs"}
                </p>
              </div>
              <ol>
                {journey.resolution.steps.map((step) => <li key={step}>{step}</li>)}
              </ol>
            </div>
          )}

          {/* LangGraph hierarchical topology */}
          <div style={{ marginBottom: 24 }}>
            <div style={{ marginBottom: 12 }}>
              <p className="eyebrow">Graph Topology</p>
              <h3 style={{ margin: 0, fontSize: 16 }}>LangGraph agent state machine</h3>
              <p style={{ fontSize: 12, color: "var(--text-secondary, #64748b)", marginTop: 4 }}>
                Router node dispatches to each agent based on shared state. Evidence agents run in parallel cluster.
                RCA feeds into a bounded self-debate loop with confidence-gated retry.
              </p>
            </div>
            <LangGraphTopology incident={incident} completedSteps={completedSteps} />
          </div>

          {/* Step-level cards */}
          <div style={{ marginBottom: 12 }}>
            <p className="eyebrow">Step Timeline</p>
            <h3 style={{ margin: 0, fontSize: 16 }}>Agent step completion</h3>
          </div>
          <div className="flow-map" aria-label="Agent workflow map">

            {flowState.nodes.map((node, index) => (
              <FlowNodeCard key={node.step.key} node={node} isLast={index === flowState.nodes.length - 1} />
            ))}
          </div>

          <div className="debate-panel">
            <div className="movement-heading">
              <div>
                <p className="eyebrow">Bounded Self-Debate</p>
                <h3>Proposal, critique, revision, and judgment</h3>
              </div>
              <span className="count-token">{debateSpans.length} debate turns</span>
            </div>
            {debateSpans.length ? (
              <div className="debate-table-wrap">
                <table className="debate-table">
                  <thead><tr><th>Round</th><th>Role</th><th>Agent</th><th>Decision / critique</th><th>Source</th></tr></thead>
                  <tbody>
                    {debateSpans.map((span, index) => (
                      <tr key={`${span.span_id}-debate-${index}`}>
                        <td>{span.debate_round || 1}</td>
                        <td>{span.debate_role}</td>
                        <td>{span.agent}</td>
                        <td>{span.reasoning || span.action}</td>
                        <td>{span.source}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <p className="empty-note">Debate agents will appear after the RCA proposer completes.</p>}
          </div>

          <div className="flow-inspector">
            <div>
              <p className="eyebrow">Runtime Evidence</p>
              <h3>How to read this map</h3>
              <p>
                Each node is derived from <code>completed_steps</code>, <code>current_status</code>, and the span-like
                <code> agent_invocations</code> trace. Router and memory events appear under the node they influenced.
              </p>
            </div>
            <div className="flow-legend">
              <span><i className="legend-dot complete" /> Complete</span>
              <span><i className="legend-dot active" /> Active</span>
              <span><i className="legend-dot review" /> Review</span>
              <span><i className="legend-dot pending" /> Pending</span>
            </div>
          </div>
        </section>
      )}

      {tab === "evidence" && (
        <section id="incidents" className="content-section">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Grounded Evidence</p>
              <h2>Claims to Raw Facts</h2>
            </div>
            <span className="count-token">{evidenceRefs.length} refs</span>
          </div>
          <div className="evidence-stack">
            {evidenceRefs.map((ref, index) => {
              const raw = ref.evidence_id ? incident.evidence_catalog?.[ref.evidence_id] : undefined;
              return (
                <article className="evidence-card" key={`${ref.evidence_id}-${index}`}>
                  <div>
                    <strong>{ref.claim}</strong>
                    <span>{ref.evidence_type}</span>
                  </div>
                  <code>{ref.evidence_id}</code>
                  <pre>{JSON.stringify(raw || {}, null, 2)}</pre>
                </article>
              );
            })}
          </div>
        </section>
      )}

      {tab === "review" && (
        <section id="review" className="content-section">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Human Governance</p>
              <h2>Review and Actions</h2>
            </div>
            <button className="ghost-button" onClick={refresh}><RefreshCw size={16} /> Refresh</button>
          </div>

          <div className="review-action-grid">
            <button disabled={busy} onClick={() => mutate(() => reviewRca(incidentId, "accepted", "Accepted from Next.js command UI."))}>
              <Check size={18} /> Accept RCA
            </button>
            <button disabled={busy} onClick={() => mutate(() => reviewRca(incidentId, "rejected", "Rejected from Next.js command UI."))}>
              <X size={18} /> Reject RCA
            </button>
            <button disabled={busy} onClick={() => mutate(() => reviewRca(incidentId, "evidence_insufficient", "Evidence marked insufficient from Next.js command UI."))}>
              <CircleAlert size={18} /> Evidence Insufficient
            </button>
            <button disabled={busy} onClick={() => mutate(() => requestMoreData(incidentId, "Reviewer requested another investigation pass from Next.js command UI."))}>
              <RefreshCw size={18} /> Request More Data
            </button>
            <button disabled={busy} onClick={() => {
              const hypothesis = window.prompt("Override root cause hypothesis");
              if (hypothesis) mutate(() => overrideRootCause(incidentId, hypothesis, "Override submitted from Next.js command UI."));
            }}>
              <ShieldCheck size={18} /> Override RCA
            </button>
          </div>

          <div className="quality-grid">
            {Object.entries(gates).map(([key, value]) => (
              <div className={`quality-item ${value === true ? "quality-pass" : value === false ? "quality-fail" : ""}`} key={key}>
                <strong>{key.replaceAll("_", " ")}</strong>
                <span>{String(value)}</span>
              </div>
            ))}
          </div>

          <div className="recommendation-list">
            {recommendations.map((item, index) => (
              <div className="recommendation" key={item}>
                <span>{index + 1}. {item}</span>
                {decisions[String(index)] ? (
                  <strong>{decisions[String(index)]?.decision}</strong>
                ) : (
                  <div>
                    <button onClick={() => mutate(() => decideRemediation(incidentId, index, "approved"))}>Approve</button>
                    <button onClick={() => mutate(() => decideRemediation(incidentId, index, "rejected"))}>Reject</button>
                  </div>
                )}
              </div>
            ))}
          </div>

          <CommandAssistant
            incidentId={incidentId}
            title="Incident Copilot"
            description="Ask about the current incident, pull in runbook knowledge, and speak with the assistant in your language."
            compact
          />
        </section>
      )}

      {tab === "trace" && (
        <section id="trace" className="content-section">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Audit Trace</p>
              <h2>Agent Spans</h2>
            </div>
            <button className="ghost-button" onClick={() => getTrace(incidentId).then(setTrace)}>
              <FileJson size={16} /> Load Trace
            </button>
          </div>
          <div className="trace-list">
            {traceSpans.map((span, index) => (
              <article className="trace-row" key={`${span.span_id}-${index}`}>
                <span>{index + 1}</span>
                <div>
                  <strong>{span.agent}</strong>
                  <p>{span.reasoning || span.action}</p>
                  <code>{span.span_id}</code>
                </div>
                <small>{span.latency_ms ? `${span.latency_ms}ms` : span.source}</small>
              </article>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function FlowNodeCard({
  node,
  isLast
}: {
  node: {
    step: FlowStep;
    status: FlowStepStatus;
    spans: Array<{
      agent?: string;
      action?: string;
      reasoning?: string;
      source?: string;
      latency_ms?: number;
      span_id?: string;
    }>;
    highlight: string;
  };
  isLast: boolean;
}) {
  const Icon = getFlowIcon(node.step.icon);
  const latestSpan = node.spans[node.spans.length - 1];
  return (
    <article className={`flow-node flow-${node.status}`}>
      <div className="flow-node-shell">
        <div className="flow-node-icon"><Icon size={19} /></div>
        <div>
          <span>{node.highlight}</span>
          <h3>{node.step.title}</h3>
          <p>{node.step.description}</p>
        </div>
      </div>

      <div className="flow-node-meta">
        <strong>{node.spans.length} span{node.spans.length === 1 ? "" : "s"}</strong>
        <span>{latestSpan?.source || latestSpan?.action || "waiting"}</span>
      </div>

      {latestSpan && (
        <div className="flow-span-callout">
          <strong>{latestSpan.agent}</strong>
          <p>{latestSpan.reasoning || latestSpan.action || "Agent completed this step."}</p>
          <small>{latestSpan.latency_ms ? `${latestSpan.latency_ms}ms` : latestSpan.span_id}</small>
        </div>
      )}

      {!isLast && <div className="flow-connector" aria-hidden="true" />}
    </article>
  );
}

function buildFlowState(
  incident: Incident,
  traceSpans: Incident["agent_invocations"],
  completedSteps: string[]
) {
  const activeKey = currentFlowKey(incident);
  const nodes = flowSteps.map((step) => {
    const spans = (traceSpans || []).filter((span) => belongsToStep(span.agent || "", span.action || "", step));
    const complete = step.completedStep ? completedSteps.includes(step.completedStep) : false;
    const status = step.key === "human_review"
      ? reviewStatus(incident)
      : activeKey === step.key && !complete
        ? "active"
        : complete
          ? "complete"
          : dependencyBlocked(step, completedSteps)
            ? "pending"
            : "pending";
    return {
      step,
      status,
      spans,
      highlight: statusLabel(status)
    };
  });
  return {
    nodes,
    completedCount: nodes.filter((node) => node.status === "complete" || node.status === "review").length,
    currentLabel: readableStatus(incident.lifecycle_status || incident.current_status || "opened")
  };
}

function belongsToStep(agent: string, action: string, step: FlowStep) {
  if (step.agents.includes(agent)) return true;
  if (agent === "router_agent" && action.includes(step.key.replace("_analysis", ""))) return true;
  return step.agents.some((stepAgent) => action.includes(stepAgent.replace("_agent", "")));
}

function currentFlowKey(incident: Incident) {
  const status = incident.current_status || "";
  if (status.includes("load")) return "load_data";
  if (status.includes("log")) return "log_analysis";
  if (status.includes("metric")) return "metrics_analysis";
  if (status.includes("debate") || status.includes("debated")) return "rca_debate";
  if (status.includes("rca") || status.includes("root")) return "rca_analysis";
  if (status.includes("impact")) return "business_impact";
  if (status.includes("summary") || status === "complete") return "summary";
  return "";
}

function reviewStatus(incident: Incident): FlowStepStatus {
  const lifecycle = (incident.lifecycle_status || "").toLowerCase();
  if (lifecycle.includes("review") || lifecycle.includes("approval")) return "review";
  if ((incident.review_events || []).length > 0) return "complete";
  if (incident.current_status === "complete") return "review";
  return "pending";
}

function dependencyBlocked(step: FlowStep, completedSteps: string[]) {
  const stepIndex = flowSteps.findIndex((item) => item.key === step.key);
  return flowSteps.slice(0, stepIndex).some((item) => item.completedStep && !completedSteps.includes(item.completedStep));
}

function statusLabel(status: FlowStepStatus) {
  const labels: Record<FlowStepStatus, string> = {
    complete: "Complete",
    active: "Running now",
    pending: "Pending",
    blocked: "Blocked",
    review: "Needs human review"
  };
  return labels[status];
}

function readableStatus(value: string) {
  return value.replaceAll("_", " ");
}

function getFlowIcon(icon: FlowStep["icon"]) {
  const icons = {
    database: Database,
    logs: FileSearch,
    metrics: Gauge,
    rca: Target,
    debate: SearchCheck,
    impact: Users,
    summary: BadgeCheck,
    review: ShieldCheck
  };
  return icons[icon];
}


function LangGraphTopology({ incident, completedSteps }: { incident: Incident; completedSteps: string[] }) {
  const done = new Set(completedSteps);
  const status = incident.current_status || "";
  const lifecycle = incident.lifecycle_status || "";

  function nodeStatus(step: string): "complete" | "active" | "pending" | "review" {
    if (done.has(step)) return "complete";
    if (status.includes(step.replace("_", "")) || status.includes(step)) return "active";
    if (lifecycle.includes("review") && step === "human_approval") return "review";
    return "pending";
  }

  // Color scheme per status
  const C = {
    complete: { fill: "#0d3d2e", stroke: "#22c55e", text: "#22c55e" },
    active:   { fill: "#0c2a3f", stroke: "#38bdf8", text: "#38bdf8" },
    review:   { fill: "#2d1f0a", stroke: "#f59e0b", text: "#f59e0b" },
    pending:  { fill: "#1e293b", stroke: "#334155", text: "#64748b" },
  };

  // Node renderer
  function N({ x, y, w = 120, h = 44, label, sub, step, router = false }: {
    x: number; y: number; w?: number; h?: number; label: string; sub?: string; step: string; router?: boolean;
  }) {
    const s = nodeStatus(step);
    const c = C[s];
    return (
      <g className={`lg-node lg-node-${s}`}>
        <rect x={x} y={y} width={w} height={h} rx={router ? 22 : 10} ry={router ? 22 : 10}
          fill={c.fill} stroke={c.stroke} strokeWidth={s !== "pending" ? 2 : 1} />
        <text x={x + w / 2} y={y + 16} textAnchor="middle" fontSize={10} fill="#e2e8f0" fontWeight={600}>
          {label}
        </text>
        {sub && (
          <text x={x + w / 2} y={y + 30} textAnchor="middle" fontSize={8} fill={c.text}>
            {sub}
          </text>
        )}
      </g>
    );
  }

  // Arrow helper
  function Arrow({ d, color = "#334155", dash = false }: { d: string; color?: string; dash?: boolean }) {
    return (
      <path d={d} fill="none" stroke={color} strokeWidth={1.5}
        strokeDasharray={dash ? "5,3" : undefined}
        markerEnd="url(#lg-arrow)" opacity={0.8} />
    );
  }

  const W = 900; const H = 520;
  // Layout coordinates (x, y) — designed as a proper DAG
  const nodes = {
    router:    { x: 390, y: 20  },
    commander: { x: 390, y: 100 },
    // Evidence cluster (parallel)
    logs:      { x: 160, y: 210 },
    metrics:   { x: 390, y: 210 },
    deploy:    { x: 620, y: 210 },
    // RCA + debate loop
    rca:       { x: 310, y: 310 },
    debate:    { x: 510, y: 310 },
    retry:     { x: 640, y: 380 },
    // Post-RCA
    impact:    { x: 160, y: 410 },
    recovery:  { x: 310, y: 410 },
    summary:   { x: 460, y: 410 },
    approval:  { x: 610, y: 410 },
    learning:  { x: 390, y: 490 },
  };

  const cx = (k: keyof typeof nodes, off = 60) => nodes[k].x + off;
  const cy = (k: keyof typeof nodes, off = 22) => nodes[k].y + off;

  return (
    <div style={{ overflowX: "auto", background: "var(--surface-raised, #0f172a)", borderRadius: 16, padding: 16 }}>
      <p style={{ fontSize: 11, color: "#64748b", marginBottom: 8, fontFamily: "monospace" }}>
        ↳ LangGraph topology · Central router dispatches to each agent · Confidence retry loop on RCA
      </p>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block", minWidth: 640 }} role="img" aria-label="LangGraph agent topology">
        <defs>
          <marker id="lg-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L0,6 L8,3 z" fill="#475569" />
          </marker>
          <marker id="lg-arrow-green" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L0,6 L8,3 z" fill="#22c55e" />
          </marker>
          <marker id="lg-arrow-amber" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L0,6 L8,3 z" fill="#f59e0b" />
          </marker>
        </defs>

        {/* PARALLEL CLUSTER backdrop */}
        <rect x={130} y={195} width={540} height={70} rx={14}
          fill="#1e293b" stroke="#334155" strokeWidth={1} strokeDasharray="6,3" opacity={0.6} />
        <text x={400} y={190} textAnchor="middle" fontSize={9} fill="#64748b" fontWeight={600}>
          PARALLEL EVIDENCE COLLECTION
        </text>

        {/* ROUTER → COMMANDER */}
        <Arrow d={`M ${cx("router")} ${cy("router",44)} L ${cx("commander")} ${cy("commander",0)}`} color="#38bdf8" />

        {/* COMMANDER → parallel cluster */}
        <Arrow d={`M ${cx("commander")} ${cy("commander",44)} L ${cx("logs")} ${cy("logs",0)}`} color="#475569" />
        <Arrow d={`M ${cx("commander")} ${cy("commander",44)} L ${cx("metrics")} ${cy("metrics",0)}`} color="#475569" />
        <Arrow d={`M ${cx("commander")} ${cy("commander",44)} L ${cx("deploy")} ${cy("deploy",0)}`} color="#475569" />

        {/* Parallel cluster → RCA */}
        <Arrow d={`M ${cx("logs")} ${cy("logs",44)} L ${cx("rca")} ${cy("rca",0)}`} color="#475569" />
        <Arrow d={`M ${cx("metrics")} ${cy("metrics",44)} L ${cx("rca")} ${cy("rca",0)}`} color="#475569" />
        <Arrow d={`M ${cx("deploy")} ${cy("deploy",44)} L ${cx("rca")} ${cy("rca",0)}`} color="#475569" />

        {/* RCA → Debate */}
        <Arrow d={`M ${cx("rca",120)} ${cy("rca")} L ${cx("debate",0)} ${cy("debate")}`} color="#a78bfa" />

        {/* Debate → Retry loop (low conf) */}
        <Arrow d={`M ${cx("debate",120)} ${cy("debate")} L ${cx("retry",0)} ${cy("retry")}`} color="#f59e0b" dash={true} />
        {/* Retry → back to debate */}
        <Arrow d={`M ${cx("retry")} ${cy("retry",0)} C ${cx("retry",60)} ${cy("debate",80)}, ${cx("debate",80)} ${cy("debate",80)}, ${cx("debate",80)} ${cy("debate",44)}`}
          color="#f59e0b" dash={true} />

        {/* Debate → post-RCA (high conf) */}
        <Arrow d={`M ${cx("debate")} ${cy("debate",44)} L ${cx("impact")} ${cy("impact",0)}`} color="#22c55e" />
        <Arrow d={`M ${cx("impact",120)} ${cy("impact")} L ${cx("recovery",0)} ${cy("recovery")}`} color="#475569" />
        <Arrow d={`M ${cx("recovery",120)} ${cy("recovery")} L ${cx("summary",0)} ${cy("summary")}`} color="#475569" />
        <Arrow d={`M ${cx("summary",120)} ${cy("summary")} L ${cx("approval",0)} ${cy("approval")}`} color="#f59e0b" />

        {/* Approval → Learning */}
        <Arrow d={`M ${cx("approval")} ${cy("approval",44)} C ${cx("approval")} ${cy("learning",0)-30}, ${cx("learning")} ${cy("learning",0)-30}, ${cx("learning")} ${cy("learning",0)}`}
          color="#475569" />

        {/* Router re-entry arcs (subtle) from all nodes */}
        {/* Labels on key edges */}
        <text x={440} y={308} fontSize={8} fill="#a78bfa">Propose</text>
        <text x={540} y={370} fontSize={8} fill="#f59e0b">Low conf retry</text>
        <text x={380} y={408} fontSize={8} fill="#22c55e">High conf ✓</text>
        <text x={580} y={408} fontSize={8} fill="#f59e0b">HITL gate</text>

        {/* NODES */}
        <N x={nodes.router.x} y={nodes.router.y} label="Router" sub="dispatcher" step="route" router={true} w={120} />
        <N x={nodes.commander.x} y={nodes.commander.y} label="Incident Commander" sub="#1 triage + context" step="incident_commander" w={140} />
        <N x={nodes.logs.x} y={nodes.logs.y} label="Log Analysis" sub="#2 error patterns" step="log_analysis" />
        <N x={nodes.metrics.x} y={nodes.metrics.y} label="Metrics Analysis" sub="#3 baselines" step="metrics_analysis" />
        <N x={nodes.deploy.x} y={nodes.deploy.y} label="Deployment Δ" sub="#4 correlation" step="deployment_analysis" />
        <N x={nodes.rca.x} y={nodes.rca.y} label="Root Cause" sub="#5 hypothesis" step="rca_analysis" />
        <N x={nodes.debate.x} y={nodes.debate.y} label="Challenge + Judge" sub="critics & reviser" step="rca_debate" />
        <N x={nodes.retry.x} y={nodes.retry.y} label="Request More Data" sub="confidence retry" step="request_more_data" w={130} />
        <N x={nodes.impact.x} y={nodes.impact.y} label="Business Impact" sub="#6 users / revenue" step="business_impact" />
        <N x={nodes.recovery.x} y={nodes.recovery.y} label="Recovery Plan" sub="#7 steps + risk" step="recovery_recommendations" />
        <N x={nodes.summary.x} y={nodes.summary.y} label="Exec Summary" sub="#8 LLM narrative" step="summary" />
        <N x={nodes.approval.x} y={nodes.approval.y} label="Human Approval" sub="HITL gate" step="human_approval" />
        <N x={nodes.learning.x} y={nodes.learning.y} label="Learning + Memory" sub="store lessons" step="learning" />

        {/* END node */}
        <circle cx={390} cy={510} r={10} fill="#0d3d2e" stroke="#22c55e" strokeWidth={2} />
        <text x={390} y={514} textAnchor="middle" fontSize={8} fill="#22c55e" fontWeight={700}>END</text>
      </svg>

      {/* Legend */}
      <div style={{ display: "flex", gap: 20, marginTop: 12, fontSize: 11, color: "#64748b", flexWrap: "wrap" }}>
        {([["#22c55e", "Complete"], ["#38bdf8", "Active"], ["#f59e0b", "Review / HITL"], ["#334155", "Pending"]] as [string, string][]).map(([color, label]) => (
          <span key={label} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 10, height: 10, borderRadius: "50%", background: color, display: "inline-block" }} />
            {label}
          </span>
        ))}
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 20, height: 1.5, background: "#f59e0b", display: "inline-block", borderTop: "2px dashed #f59e0b" }} />
          Confidence retry loop
        </span>
      </div>
    </div>
  );
}




function MissingIncidentPanel({
  incidentId,
  message,
  openLatestIncident
}: {
  incidentId: string;
  message: string | null;
  openLatestIncident: () => Promise<void>;
}) {
  return (
    <section className="missing-incident-panel">
      <div className="missing-incident-icon"><CircleAlert size={28} /></div>
      <p className="eyebrow">Incident Not Found</p>
      <h1>This investigation is no longer in backend memory.</h1>
      <p>
        {message || "The backend returned 404 for this incident."} This usually happens after restarting Uvicorn,
        because demo incidents are kept in the active Python process.
      </p>
      <code>{incidentId}</code>
      <div className="button-row">
        <Link className="ghost-button" href="/"><ArrowLeft size={16} /> Back to dashboard</Link>
        <button className="ghost-button" onClick={openLatestIncident}><RefreshCw size={16} /> Open latest incident</button>
      </div>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function SummaryBlock({ title, text }: { title: string; text: string }) {
  return (
    <article className="summary-block">
      <h3>{title}</h3>
      <pre>{text}</pre>
    </article>
  );
}

