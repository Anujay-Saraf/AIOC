import type { AssistantAnswer, Connector, ConnectorCatalogItem, Incident, IncidentAnalytics, IncidentJourneyResponse, JarvisStatus, KnowledgeGraph, KnowledgeHit, TraceExport } from "./types";

// Prefer explicit NEXT_PUBLIC_API_BASE; otherwise default to local backend for dev.
const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export class ApiError extends Error {
  status: number;

  constructor(status: number, statusText: string) {
    super(`${status} ${statusText}`);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {})
    },
    cache: "no-store"
  });
  if (!response.ok) {
    throw new ApiError(response.status, response.statusText);
  }
  return response.json() as Promise<T>;
}

export function listIncidents() {
  return request<Incident[]>("/api/incidents");
}

export function getIncident(id: string) {
  return request<Incident>(`/api/incidents/${id}`);
}

export function getTrace(id: string) {
  return request<TraceExport>(`/api/incidents/${id}/trace`);
}

export function getIncidentJourney(id: string) {
  return request<IncidentJourneyResponse>(`/api/incidents/${id}/journey`);
}

export function triggerIncident(payload: {
  timestamp: string;
  service: string;
  alert_description: string;
  severity: string;
}) {
  return request<Incident>("/api/incidents/trigger", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function resetIncidents() {
  return request<{ cleared: number }>("/api/incidents/reset", {
    method: "POST",
    body: JSON.stringify({ confirm: "RESET_DEMO_INCIDENTS" })
  });
}

export function reviewRca(id: string, decision: string, reason: string) {
  return request<Incident>(`/api/incidents/${id}/review/rca`, {
    method: "POST",
    body: JSON.stringify({ decision, reason, actor: "next-web" })
  });
}

export function requestMoreData(id: string, reason: string) {
  return request<Incident>(`/api/incidents/${id}/review/request-more-data`, {
    method: "POST",
    body: JSON.stringify({ reason, actor: "next-web" })
  });
}

export function overrideRootCause(id: string, hypothesis: string, reason: string) {
  return request<Incident>(`/api/incidents/${id}/review/override-root-cause`, {
    method: "POST",
    body: JSON.stringify({ hypothesis, reason, actor: "next-web" })
  });
}

export function decideRemediation(id: string, stepIndex: number, decision: "approved" | "rejected") {
  return request<Incident>(`/api/incidents/${id}/remediation/${stepIndex}/decision`, {
    method: "POST",
    body: JSON.stringify({ decision, actor: "next-web" })
  });
}

export function askIncident(id: string, question: string) {
  return request<AssistantAnswer>(`/api/incidents/${id}/ask`, {
    method: "POST",
    body: JSON.stringify({ question })
  });
}

export function askAssistant(payload: {
  message: string;
  incident_id?: string;
  language_code?: string;
}) {
  return request<AssistantAnswer>("/api/assistant/chat", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function getJarvisStatus() {
  return request<JarvisStatus>("/api/jarvis/status");
}

export function applyOllamaModel(payload: { model: string; endpoint?: string }) {
  return request<{ connector: Connector; ollama: JarvisStatus["ollama"] }>("/api/admin/ollama/apply", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function testModelConnection(prompt?: string) {
  return request<{ ok: boolean; model: string; provider: string; reply?: string; error?: string }>(
    "/api/admin/model/test",
    {
      method: "POST",
      body: JSON.stringify({ prompt: prompt ?? "Reply with exactly: AIOC online" }),
    }
  );
}

export function searchKnowledge(query: string, incidentId?: string) {
  const suffix = new URLSearchParams({ q: query });
  if (incidentId) suffix.set("incident_id", incidentId);
  return request<{ query: string; results: KnowledgeHit[]; confidence: number }>(`/api/knowledge/search?${suffix.toString()}`);
}

export function uploadKnowledge(file: File, text?: string) {
  const formData = new FormData();
  formData.append("file", file);
  if (text) {
    formData.append("text", text);
  }
  return fetch(`${API_BASE}/api/knowledge/upload`, {
    method: "POST",
    body: formData,
  }).then(async (response) => {
    if (!response.ok) {
      const errorBody = await response.text();
      throw new Error(errorBody || response.statusText);
    }
    return response.json();
  });
}

export function uploadIncidentLog(file: File, text?: string, sourceConnectorId?: string) {
  const formData = new FormData();
  formData.append("file", file);
  if (text) {
    formData.append("text", text);
  }
  if (sourceConnectorId) {
    formData.append("source_connector_id", sourceConnectorId);
  }
  return fetch(`${API_BASE}/api/incidents/upload-log`, {
    method: "POST",
    body: formData,
  }).then(async (response) => {
    if (!response.ok) {
      const errorBody = await response.text();
      throw new Error(errorBody || response.statusText);
    }
    return response.json();
  });
}

export function postmortemUrl(id: string) {
  return `${API_BASE}/api/incidents/${id}/postmortem`;
}

export function getConnectorCatalog() {
  return request<{ connectors: ConnectorCatalogItem[] }>("/api/admin/connectors/catalog");
}

export function listConnectors() {
  return request<{ connectors: Connector[] }>("/api/admin/connectors");
}

export function saveConnector(payload: Partial<Connector>) {
  return request<Connector>("/api/admin/connectors", { method: "POST", body: JSON.stringify(payload) });
}

export function removeConnector(id: string) {
  return request<{ deleted: string }>(`/api/admin/connectors/${id}`, { method: "DELETE" });
}

export function checkConnector(id: string) {
  return request<Connector>(`/api/admin/connectors/${id}/heartbeat`, { method: "POST" });
}

export function getIncidentAnalytics(period: "day" | "week" | "month") {
  return request<IncidentAnalytics>(`/api/analytics/incidents?period=${period}`);
}

export function getKnowledgeGraph() {
  return request<KnowledgeGraph>("/api/knowledge-graph");
}

export function getPipelinesHealth() {
  return request<Record<string, { name: string; status: string; last_checked: string | null; latency_ms: number | null; error_rate: number | null; message: string }>>("/api/pipelines/health");
}

export function simulatePipelines(pipeline?: string) {
  // Try POST first; if it fails (405/other) fallback to GET.
  return fetch(`${API_BASE}/api/pipelines/simulate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pipeline: pipeline ?? "all" }),
    cache: "no-store",
  }).then(async (resp) => {
    if (resp.ok) return resp.json();
    // fallback to GET
    const q = new URLSearchParams();
    if (pipeline) q.set("pipeline", pipeline);
    const g = await fetch(`${API_BASE}/api/pipelines/simulate?${q.toString()}`, { cache: "no-store" });
    if (!g.ok) throw new ApiError(g.status, g.statusText);
    return g.json();
  });
}

export function pipelineSimulatorStatus() {
  return request<{ running: boolean }>("/api/pipelines/simulator/status");
}

export function controlPipelineSimulator(action: "start" | "stop") {
  return request<{ running: boolean }>("/api/pipelines/simulator", { method: "POST", body: JSON.stringify({ action }) });
}
