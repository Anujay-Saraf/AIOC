from typing import Any, Dict, List

from agents import IncidentState
from agents.connectors import get_telemetry_provider
from agents.evidence import attach_evidence_ids, build_evidence_catalog
from agents.log_analysis import log_analysis
from agents.telemetry import Timer, record_invocation


def request_more_data(state: IncidentState) -> IncidentState:
    """Low-confidence loop: gather deeper evidence, then send the state back
    through RCA. Re-fetches logs and deployment history, widens the log scan,
    and records exactly what extra data it decided to pull."""
    t = Timer.begin()
    state.current_status = "requesting_deeper_analysis"
    provider = get_telemetry_provider()

    data_requested: List[str] = []

    fresh_logs: List[Dict[str, Any]] = provider.logs(state.service, state.timestamp)
    if len(fresh_logs) > len(state.raw_logs):
        data_requested.append(f"fetched {len(fresh_logs) - len(state.raw_logs)} additional log entries")
    state.raw_logs = fresh_logs or state.raw_logs

    fresh_deployments: List[Dict[str, Any]] = provider.deployments(state.service, state.timestamp)
    if fresh_deployments and not state.deployment_changes:
        data_requested.append(f"fetched {len(fresh_deployments)} deployment records")
    state.deployment_changes = fresh_deployments or state.deployment_changes

    attach_evidence_ids(
        state.service, state.raw_logs, state.raw_metrics, state.deployment_changes
    )
    state.evidence_catalog = build_evidence_catalog(
        state.raw_logs, state.raw_metrics, state.deployment_changes
    )

    state.log_anomalies = []
    state = log_analysis(state)
    data_requested.append(f"re-ran log analysis: {len(state.log_anomalies)} anomalies")

    state.completed_steps = [s for s in state.completed_steps if s != "rca_analysis"]

    record_invocation(
        state,
        agent="request_more_data_agent",
        action="request_deeper_analysis",
        source=f"provider:{provider.name}",
        reasoning=(
            f"RCA confidence {state.rca_confidence:.2f} is below the 0.70 threshold; "
            "gathering more evidence before re-running RCA"
        ),
        findings={
            "data_requested": data_requested,
            "evidence_catalog_size": len(state.evidence_catalog),
        },
        latency_ms=t.ms(),
    )

    return state
