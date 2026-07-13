from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class IncidentState:
    # --- Identity ---
    incident_id: str
    timestamp: str
    alert_description: str
    service: str
    trace_id: str = ""
    severity: str = "unknown"

    # --- Lifecycle (single field — no more agent_status / lifecycle_status split) ---
    lifecycle_status: str = "opened"   # opened | investigating | needs_human_review | resolved

    # --- Raw telemetry (inputs to investigation agents) ---
    raw_logs: List[Dict[str, Any]] = field(default_factory=list)
    raw_metrics: List[Dict[str, Any]] = field(default_factory=list)
    deployment_changes: List[Dict[str, Any]] = field(default_factory=list)

    # --- AIOC Agent #2: Log Analysis ---
    log_anomalies: List[Dict[str, Any]] = field(default_factory=list)
    log_context_cache: Dict[str, Any] = field(default_factory=dict)

    # --- AIOC Agent #3: Metrics Analysis ---
    metric_anomalies: List[Dict[str, Any]] = field(default_factory=list)

    # --- AIOC Agent #4: Deployment Analysis (NEW dedicated agent) ---
    deployment_analysis: Dict[str, Any] = field(default_factory=dict)

    # --- AIOC Agent #5: Root Cause Analysis ---
    root_cause: Optional[Dict[str, Any]] = None
    rca_confidence: float = 0.0
    analysis_iterations: int = 0
    max_iterations: int = 10
    debate_rounds: List[Dict[str, Any]] = field(default_factory=list)

    # --- AIOC Agent #6: Business Impact ---
    affected_users: int = 0
    estimated_revenue_impact_per_minute: float = 0.0
    estimated_cost_impact_per_minute: float = 0.0
    revenue_impact_justification: Dict[str, Any] = field(default_factory=dict)
    business_risk_level: str = "unknown"
    blast_radius: Dict[str, Any] = field(default_factory=dict)

    # --- AIOC Agent #7: Recovery Recommendations (NEW dedicated agent) ---
    recovery_recommendations: List[str] = field(default_factory=list)  # flat list (backward compat)
    recovery_plan: Dict[str, Any] = field(default_factory=dict)       # structured plan

    # --- AIOC Agent #8: Executive Summary ---
    engineering_summary: str = ""
    executive_summary: str = ""

    # --- Service context (set by Incident Commander) ---
    service_profile: Dict[str, Any] = field(default_factory=dict)
    ownership: Dict[str, Any] = field(default_factory=dict)
    escalation_path: List[str] = field(default_factory=list)
    runbooks: List[str] = field(default_factory=list)
    rollback_plan: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    upstream_services: List[str] = field(default_factory=list)
    environment: Dict[str, Any] = field(default_factory=dict)  # kept for backward compat
    context_metadata: Dict[str, Any] = field(default_factory=dict)  # kept for backward compat

    # --- Knowledge retrieval ---
    retrieval_results: List[Dict[str, Any]] = field(default_factory=list)
    evidence_catalog: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    compact_contexts: List[Dict[str, Any]] = field(default_factory=list)
    evidence_citations: List[Dict[str, Any]] = field(default_factory=list)  # RCA claims + KG citations

    # --- Knowledge-Graph (RAG) integration ---
    kg_similar_incidents: List[str] = field(default_factory=list)  # cited facts about similar past incidents
    historical_impact_note: str = ""  # note on historical revenue/cost impact of similar incidents

    # --- Memory & learning ---
    similar_incidents: List[Dict[str, Any]] = field(default_factory=list)
    review_events: List[Dict[str, Any]] = field(default_factory=list)
    remediation_policy: Dict[str, Any] = field(default_factory=dict)
    quality_gates: Dict[str, Any] = field(default_factory=dict)
    kpi_guardrails: Dict[str, Any] = field(default_factory=dict)
    stakeholder_updates: Dict[str, Any] = field(default_factory=dict)
    troubleshooting_plan: List[str] = field(default_factory=list)
    escalation_summary: str = ""

    # --- Audit trail (append-only across all agents) ---
    agent_invocations: List[Dict[str, Any]] = field(default_factory=list)
    agent_status: str = "initial"  # kept for backward compat with app.py

    # --- Graph control (List[str], not Set — JSON-serializable for checkpointing) ---
    completed_steps: List[str] = field(default_factory=list)
    current_status: str = "initial"
    next_action: str = ""

    # --- Telemetry ---
    span_seq: int = 0
    current_parent_span_id: str = ""
