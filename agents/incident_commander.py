from typing import Any
from datetime import datetime

from agents import IncidentState
from agents.connectors import get_telemetry_provider
from agents.evidence import attach_evidence_ids, build_evidence_catalog
from agents.telemetry import Timer, record_invocation
from agents.context_builder import build_incident_context, service_profile
from agents.knowledge_base import kg_query


def incident_commander(state: IncidentState) -> IncidentState:
    t = Timer.begin()
    timestamp: str = state.timestamp or datetime.now().isoformat()
    provider = get_telemetry_provider()

    if not state.raw_logs:
        state.raw_logs = provider.logs(state.service, timestamp)
    if not state.raw_metrics:
        state.raw_metrics = provider.metrics(state.service, timestamp)
    if not state.deployment_changes:
        state.deployment_changes = provider.deployments(state.service, timestamp)

    profile = service_profile(state.service)

    state.service_profile = profile
    state.ownership = profile.get("owner", {})
    state.environment = profile.get("environment", {})
    state.dependencies = profile.get("dependencies", [])
    state.upstream_services = profile.get("upstream_services", [])
    state.runbooks = profile.get("runbooks", [])
    state.escalation_path = profile.get("escalation_path", [])
    state.rollback_plan = profile.get("rollback", {})
    state.blast_radius = {
        "services": [*state.upstream_services, *state.dependencies],
        "estimated_scope": "high" if len(state.upstream_services) + len(state.dependencies) >= 5 else "medium" if state.upstream_services or state.dependencies else "low",
        "reason": "Derived from service catalog dependencies and upstream consumers.",
    }
    state.context_metadata = build_incident_context({
        "incident_id": state.incident_id,
        "service": state.service,
        "severity": state.severity,
        "alert_description": state.alert_description,
        "deployment_changes": state.deployment_changes,
        "business_risk_level": state.business_risk_level,
        "source_connector_id": getattr(state, "source_connector_id", None),
        "similar_incidents": state.similar_incidents,
    })

    attach_evidence_ids(
        state.service, state.raw_logs, state.raw_metrics, state.deployment_changes
    )
    state.evidence_catalog = build_evidence_catalog(
        state.raw_logs, state.raw_metrics, state.deployment_changes
    )

    # --- Non-blocking knowledge-graph recall of similar past incidents -------
    try:
        kg_result = kg_query(
            f"similar past incidents for service {state.service}",
            incident_context={
                "service": state.service,
                "alert_description": state.alert_description,
                "incident_id": state.incident_id,
            },
        )
        state.kg_similar_incidents = [f["fact"] for f in kg_result.get("facts", [])]
    except Exception:
        state.kg_similar_incidents = []

    record_invocation(
        state,
        agent="incident_commander",
        action="load_incident_data",
        source=f"provider:{provider.name}",
        findings={
            "logs_loaded": len(state.raw_logs),
            "metrics_loaded": len(state.raw_metrics),
            "deployments_loaded": len(state.deployment_changes),
            "evidence_catalog_size": len(state.evidence_catalog),
            "owner_team": state.ownership.get("team"),
            "environment": state.environment,
            "dependencies": state.dependencies,
            "blast_radius": state.blast_radius,
        },
        latency_ms=t.ms(),
    )

    return state
