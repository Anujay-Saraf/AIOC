export type SourceMap = Record<string, unknown>;

export type EvidenceRef = {
  claim?: string;
  evidence_id?: string;
  evidence_type?: string;
};

export type RootCause = {
  hypothesis?: string;
  confidence?: number;
  supporting_evidence?: string[];
  supporting_evidence_refs?: EvidenceRef[];
  ruled_out_hypotheses?: Array<{ hypothesis?: string; reason?: string }>;
  deploy_correlation?: string;
  confidence_breakdown?: Record<string, number | string | boolean | null>;
};

export type AgentInvocation = {
  agent?: string;
  action?: string;
  source?: string;
  timestamp?: string;
  reasoning?: string;
  findings?: Record<string, unknown>;
  span_id?: string;
  parent_span_id?: string;
  latency_ms?: number;
  llm?: Record<string, unknown>;
  debate_round?: number;
  debate_role?: string;
};

export type ReviewEvent = {
  timestamp?: string;
  actor?: string;
  action?: string;
  decision?: string;
  reason?: string;
};

export type KnowledgeHit = {
  title?: string;
  source_path?: string;
  kind?: string;
  content?: string;
  score?: number;
  citation?: string;
};

export type AssistantAnswer = {
  answer: string;
  confidence?: number;
  citations?: KnowledgeHit[];
  follow_ups?: string[];
  language?: string;
  source?: string;
  route?: AssistantRoute;
  routing?: AssistantRoute;
  knowledge?: {
    query?: string;
    confidence?: number;
    results?: KnowledgeHit[];
  };
};

export type AssistantRoute = {
  tier?: string;
  cache_hit?: boolean;
  provider?: string;
  model?: string;
  fallback_reason?: string;
  latency_ms?: number;
  context_fingerprint?: string;
  cache_match?: string;
  cache_similarity?: number;
};

export type JarvisStatus = {
  status?: string;
  local_model?: {
    available?: boolean;
    provider?: string;
    model?: string;
    endpoint?: string;
  };
  cache?: {
    enabled?: boolean;
    backend?: string;
    entries?: number;
    hit_rate?: number;
    active_entries?: number;
    expired_entries?: number;
    total_entries?: number;
    hits?: number;
  };
  online_fallback?: {
    available?: boolean;
    provider?: string;
    model?: string;
  };
  routing?: {
    local_available?: boolean;
    cache_enabled?: boolean;
    provider?: string;
    model?: string;
    order?: string[];
    local?: {
      enabled?: boolean;
      provider?: string;
      model?: string;
      source?: string;
    };
    online?: {
      configured?: boolean;
      provider?: string;
      model?: string;
      source?: string;
    };
  };
  ollama?: {
    enabled?: boolean;
    reachable?: boolean;
    feasible?: boolean;
    model?: string;
    model_available?: boolean;
    source?: string;
    available_models?: string[];
    detail?: string;
  };
  background?: {
    incident_count?: number;
    connector_count?: number;
    analytics?: Record<string, unknown>;
  };
  model?: string;
  provider?: string;
  llm_ready?: boolean;
};

export type Incident = {
  incident_id: string;
  trace_id?: string;
  timestamp?: string;
  alert_description?: string;
  service?: string;
  severity?: string;
  lifecycle_status?: string;
  agent_status?: string;
  current_status?: string;
  analysis_iterations?: number;
  rca_confidence?: number;
  completed_steps?: string[];
  evidence_catalog?: Record<string, SourceMap>;
  log_anomalies?: Array<Record<string, unknown>>;
  metric_anomalies?: Array<Record<string, unknown>>;
  deployment_changes?: Array<Record<string, unknown>>;
  root_cause?: RootCause | null;
  affected_users?: number;
  estimated_revenue_impact_per_minute?: number;
  estimated_cost_impact_per_minute?: number;
  revenue_impact_justification?: Record<string, unknown>;
  business_risk_level?: string;
  engineering_summary?: string;
  executive_summary?: string;
  recovery_recommendations?: string[];
  troubleshooting_plan?: string[];
  stakeholder_updates?: Record<string, string>;
  kpi_guardrails?: Record<string, unknown>;
  escalation_summary?: string;
  remediation_decisions?: Record<string, { decision?: string; decided_at?: string }>;
  similar_incidents?: Array<Record<string, unknown>>;
  agent_invocations?: AgentInvocation[];
  compact_contexts?: Array<Record<string, unknown>>;
  review_events?: ReviewEvent[];
  debate_rounds?: Array<Record<string, unknown>>;
  quality_gates?: Record<string, boolean | number | string>;
  created_at?: string;
};

export type TraceExport = {
  incident_id?: string;
  trace_id?: string;
  lifecycle_status?: string;
  agent_status?: string;
  quality_gates?: Record<string, unknown>;
  compact_contexts?: Array<Record<string, unknown>>;
  review_events?: ReviewEvent[];
  spans?: AgentInvocation[];
};

export type Connector = {
  id: string;
  name: string;
  type: string;
  enabled: boolean;
  config: Record<string, string>;
  status?: "pending" | "configured" | "online" | "error" | "disabled";
  heartbeat_detail?: string;
  last_heartbeat?: string;
};

export type ConnectorCatalogItem = {
  type: string;
  name: string;
  category: string;
  description?: string;
  fields: Array<string | ConnectorCatalogField>;
};

export type ConnectorCatalogField = {
  name: string;
  label?: string;
  type?: "text" | "password" | "url" | "number" | "select";
  placeholder?: string;
  help?: string;
  required?: boolean;
  default?: string;
  options?: Array<string | { value: string; label?: string }>;
};

export type IncidentAnalytics = {
  period: "day" | "week" | "month";
  window_days: number;
  total: number;
  resolved: number;
  impact_per_minute: number;
  active: number;
  synthetic_count?: number;
  resolution_buckets: Array<{ label: string; count: number }>;
  service_buckets: Array<{ label: string; count: number }>;
  recurring: Array<{ signature: string; count: number; service: string; resolution: string; impact_per_minute: number; incident_ids: string[] }>;
};

export type IncidentResolution = {
  summary: string;
  steps: string[];
  approval_required: boolean;
  owner?: string;
  rollback?: Record<string, unknown>;
  safety_check?: string;
  confidence?: number;
};

export type JourneyGraph = {
  nodes: Array<{
    id: string;
    label: string;
    type?: string;
    status?: string;
  }>;
  edges: Array<{
    source: string;
    target: string;
    reason?: string;
  }>;
  spans: AgentInvocation[];
};

export type IncidentJourneyResponse = {
  incident_id: string;
  journey: JourneyGraph;
  resolution: IncidentResolution;
};

export type KnowledgeGraph = {
  nodes: Array<{
    id: string;
    type: "incident" | "service" | "cause" | "resolution" | "evidence" | "entity" | string;
    label: string;
    detail?: string;
    severity?: string;
    status?: string;
    impact_per_minute?: number;
    [property: string]: unknown;
  }>;
  edges: Array<{ source: string; target: string; relation: string }>;
  backend?: string;
};
