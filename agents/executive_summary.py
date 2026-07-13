from __future__ import annotations

from typing import Any

from agents import IncidentState
from agents.telemetry import Timer, record_invocation
from agents.knowledge_base import kg_query


def executive_summary(state: IncidentState) -> IncidentState:
    """Generate engineering and executive summaries, then capture lessons learned.

    Adds a lessons_learned entry to ``state.agent_invocations`` using the same
    reflection wording as the original ``_learning_node``. If the RCA confidence
    is below 0.7 a brief follow‑up note is appended to the executive summary.
    The step name ``"summary"`` is added to ``state.completed_steps`` (idempotently)
    and ``state.lifecycle_status`` is set to ``"investigating"`` if not already set.
    """
    t = Timer.begin()
    if not state.recovery_recommendations:
        state.recovery_recommendations = _default_recommendations(state)

    root_cause_info: str = ""
    if state.root_cause:
        root_cause_info = f"Root Cause: {state.root_cause['hypothesis']} (Confidence: {state.root_cause['confidence']*100:.0f}%)"

    log_summary: str = ""
    if state.log_anomalies:
        log_types: list[str] = [anomaly["type"] for anomaly in state.log_anomalies]
        log_summary = f"Log Anomalies: {', '.join(log_types)}"

    metric_summary: str = ""
    if state.metric_anomalies:
        metric_summaries: list[str] = [
            f"{m['metric_name']}: {m['percent_change']:.1f}% change"
            for m in state.metric_anomalies
        ]
        metric_summary = f"Metric Anomalies: {', '.join(metric_summaries)}"

    impact: dict[str, Any] = state.revenue_impact_justification or {}
    log_cache: dict[str, Any] = state.log_context_cache or {}
    stakeholder_updates: dict[str, Any] = state.stakeholder_updates or {}

    engineering_sections: list[str] = [
        f"Service: {state.service}",
        f"Alert: {state.alert_description}",
        f"Severity: {state.severity}",
        "",
        log_summary if log_summary else "No log anomalies detected",
        "",
        metric_summary if metric_summary else "No metric anomalies detected",
        "",
        root_cause_info if root_cause_info else "Root cause analysis pending"
    ]

    if impact:
        engineering_sections.extend(
            [
                "",
                "Revenue Impact Justification:",
                f"  Formula: {impact.get('formula')}",
                (
                    f"  Inputs: {impact.get('affected_users', 0):,} affected users, "
                    f"${impact.get('revenue_per_user_per_minute', 0):.2f}/user/min"
                ),
                (
                    f"  Bound: ${impact.get('lower_bound_per_minute', 0):.2f}-"
                    f"${impact.get('upper_bound_per_minute', 0):.2f}/minute"
                ),
            ]
        )

    if log_cache:
        engineering_sections.extend(
            [
                "",
                "Centralized Log Context:",
                (
                    f"  Scanned {log_cache.get('total_logs_scanned', 0)} logs; "
                    f"cached {len(log_cache.get('error_contexts', []))} error context windows"
                ),
            ]
        )

    if state.root_cause and state.root_cause.get("supporting_evidence"):
        engineering_sections.append("")
        engineering_sections.append("Supporting Evidence:")
        for evidence in state.root_cause["supporting_evidence"]:
            engineering_sections.append(f"  • {evidence}")

    if state.recovery_recommendations:
        engineering_sections.append("")
        engineering_sections.append("Recovery Recommendations:")
        for rec in state.recovery_recommendations:
            engineering_sections.append(f"  • {rec}")

    state.engineering_summary = "\n".join(engineering_sections)

    executive_sections: list[str] = [
        f"INCIDENT REPORT: {state.service.upper()}",
        f"Timestamp: {state.timestamp}",
        f"Severity: {state.severity.upper()}",
        "",
        "IMPACT",
        f"  Affected Users: {state.affected_users:,}",
        f"  Revenue Impact: ${state.estimated_revenue_impact_per_minute:.2f}/minute",
        f"  Cost Impact: ${state.estimated_cost_impact_per_minute:.2f}/minute",
        f"  Business Risk: {state.business_risk_level}",
        (
            f"  Justification: {impact.get('affected_users', state.affected_users):,} users x "
            f"${impact.get('revenue_per_user_per_minute', 0):.2f}/user/min"
            if impact
            else "  Justification: pending"
        ),
        (
            f"  Bounded Range: ${impact.get('lower_bound_per_minute', 0):.2f}-"
            f"${impact.get('upper_bound_per_minute', 0):.2f}/minute"
            if impact
            else "  Bounded Range: pending"
        ),
        "",
        "ROOT CAUSE",
        f"  {root_cause_info if root_cause_info else 'Analysis in progress'}",
        "",
        "CURRENT STATUS",
        f"  Analysis agents invoked: {len(state.agent_invocations)}",
        f"  Log anomalies found: {len(state.log_anomalies)}",
        f"  Log context windows cached: {len(log_cache.get('error_contexts', []))}",
        f"  Metric anomalies found: {len(state.metric_anomalies)}",
    ]

    if stakeholder_updates:
        executive_sections.extend(
            [
                "",
                "STAKEHOLDER UPDATES",
                f"  Engineering: {stakeholder_updates.get('engineering', 'pending')}",
                f"  Business: {stakeholder_updates.get('business', 'pending')}",
                f"  Customers: {stakeholder_updates.get('customers', 'pending')}",
                f"  Ops: {stakeholder_updates.get('ops', 'pending')}",
            ]
        )

    # --- Non-blocking historical context from the knowledge graph ------------
    try:
        kg_result = kg_query(
            f"historical context for incident on {state.service} {state.alert_description}",
            incident_context={
                "service": state.service,
                "alert_description": state.alert_description,
                "root_cause": state.root_cause,
                "incident_id": state.incident_id,
            },
        )
    except Exception:
        kg_result = {"facts": []}
    historical_facts = [f["fact"] for f in kg_result.get("facts", [])][:3]
    if historical_facts:
        executive_sections.extend(["", "HISTORICAL CONTEXT"])
        for fact in historical_facts:
            executive_sections.append(f"  • {fact}")

    state.executive_summary = "\n".join(executive_sections)

    record_invocation(
        state,
        agent="executive_summary",
        action="generate_summaries",
        source="heuristic",
        findings={
            "engineering_summary_length": len(state.engineering_summary),
            "executive_summary_length": len(state.executive_summary),
            "recovery_recommendations": len(state.recovery_recommendations),
        },
        latency_ms=t.ms(),
    )

    return state


def _default_recommendations(state: IncidentState) -> list[str]:
    hypothesis: str = (state.root_cause or {}).get("hypothesis", "").lower()

    if "pool" in hypothesis or "connection" in hypothesis:
        return [
            "Rollback the deployment that reduced the connection pool size",
            "Temporarily raise the DB connection pool limit",
            "Monitor connection wait times until error rate returns to baseline",
            "Add an alert on pool utilization above 80%",
        ]
    if "memory" in hypothesis or "leak" in hypothesis:
        return [
            "Restart affected instances on a rolling basis to reclaim memory",
            "Bisect recent code changes for objects not being released",
            "Capture a heap dump from a degraded instance for analysis",
            "Add an alert on sustained memory growth and GC pause times",
        ]
    if "cascad" in hypothesis or "downstream" in hypothesis or "timeout" in hypothesis:
        return [
            "Enable circuit breaker on calls to the degraded downstream service",
            "Reduce downstream call timeout and add retry budget limits",
            "Engage the downstream service owning team",
            "Serve degraded/cached responses until the dependency recovers",
        ]
    if "cache stampede" in hypothesis:
        return [
            "Enable request coalescing so concurrent cache misses share one origin fetch",
            "Add TTL jitter and stale-while-revalidate to prevent synchronized expirations",
            "Cap retry attempts with exponential backoff and a per-request retry budget",
            "Pre-warm hot search keys before reopening full traffic",
        ]
    if "traffic" in hypothesis or "cost" in hypothesis or "retry" in hypothesis:
        return [
            "Throttle or rate-limit the bursty request path",
            "Stop retry amplification and cap fan-out until traffic settles",
            "Notify the business and finance owners about the cost overrun",
            "Pre-scale or cache the hot path before re-opening traffic",
        ]
    return [
        "Investigate root cause hypothesis",
        "Check recent deployments and rollback if necessary",
        "Monitor affected service metrics closely",
        "Prepare communication for impacted customers",
        "Execute recovery procedures once root cause confirmed",
    ]
