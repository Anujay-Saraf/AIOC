import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from agents import IncidentState

MEMORY_PATH: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "incident_memory.json",
)
POSTMORTEM_DIR: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "postmortems",
)


def _load() -> List[Dict[str, Any]]:
    try:
        with open(MEMORY_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(items: List[Dict[str, Any]]) -> None:
    with open(MEMORY_PATH, "w") as f:
        json.dump(items, f, indent=2)


def list_incident_memory() -> List[Dict[str, Any]]:
    """Return persisted completed incidents for global assistant discovery."""
    return _load()


def _postmortem_path(record: Dict[str, Any]) -> Path:
    incident_id = str(record.get("incident_id") or "unknown").strip().replace("/", "_").replace("\\", "_")
    filename = f"postmortem-{incident_id}.md"
    return Path(POSTMORTEM_DIR) / filename


def render_postmortem_markdown(record: Dict[str, Any]) -> str:
    rc: Dict[str, Any] = record.get("root_cause") or {}
    decisions: Dict[str, Any] = record.get("remediation_decisions", {})
    impact: Dict[str, Any] = record.get("revenue_impact_justification") or {}
    log_cache: Dict[str, Any] = record.get("log_context_cache") or {}
    contributing: list[Any] = record.get("contributing_factors") or []
    lessons: list[Any] = record.get("lessons_learned") or []
    preventive: list[Any] = record.get("preventive_actions") or []
    reflections: list[Any] = record.get("agent_reflections") or []
    remediation = record.get("recovery_recommendations") or []
    timeline = record.get("agent_invocations") or []
    review_events = record.get("review_events") or []

    tags = ["incident", "postmortem", str(record.get("service") or "unknown")]
    lines: List[str] = [
        "---",
        f"incident_id: {record.get('incident_id')}",
        f"title: Incident Postmortem â€” {record.get('service')}",
        f"service: {record.get('service')}",
        f"severity: {record.get('severity')}",
        f"date: {record.get('timestamp')}",
        f"tags: [{', '.join(str(tag) for tag in tags)}]",
        "---",
        "",
        f"# Incident Postmortem â€” {record.get('service')}",
        "",
        f"- **Incident ID:** {record.get('incident_id')}",
        f"- **Trace ID:** {record.get('trace_id')}",
        f"- **Lifecycle:** {record.get('lifecycle_status')}",
        f"- **Date:** {record.get('timestamp')}",
        f"- **Severity:** {record.get('severity')}",
        f"- **Alert:** {record.get('alert_description')}",
        "",
        "## Executive Summary",
        "",
        str(record.get("executive_summary") or "N/A"),
        "",
        "## Root Cause",
        "",
        f"**{rc.get('hypothesis', 'Unknown')}** (confidence: {float(rc.get('confidence') or 0) * 100:.0f}%)",
        "",
    ]
    if contributing:
        lines += ["## Contributing Factors", "", *[f"- {item}" for item in contributing], ""]
    lines += ["### Supporting Evidence", "", *[f"- {e}" for e in rc.get("supporting_evidence", [])]]
    if rc.get("supporting_evidence_refs"):
        lines += ["", "### Evidence References", "", *[f"- `{ref.get('evidence_id')}` ({ref.get('evidence_type')}): {ref.get('claim')}" for ref in rc.get("supporting_evidence_refs", [])]]
    if rc.get("confidence_breakdown"):
        lines += ["", "### Confidence Breakdown", "", *[f"- {key}: {value}" for key, value in rc.get("confidence_breakdown", {}).items()]]
    if rc.get("ruled_out_hypotheses"):
        lines += ["", "### Alternatives Considered & Ruled Out", "", *[f"- ~~{r.get('hypothesis')}~~ â€” {r.get('reason')}" for r in rc["ruled_out_hypotheses"]]]
    lines += ["", "## Business Impact", "", f"- Affected users: {record.get('affected_users', 0):,}", f"- Estimated revenue impact: ${record.get('estimated_revenue_impact_per_minute', 0):.2f}/minute", f"- Estimated cost impact: ${record.get('estimated_cost_impact_per_minute', 0):.2f}/minute", f"- Business risk level: {record.get('business_risk_level', 'unknown')}"]
    if impact:
        lines += ["", "### Impact Justification", "", f"- Affected users: {impact.get('affected_users', 0):,}", f"- Revenue per user per minute: ${impact.get('revenue_per_user_per_minute', 0):.2f}", f"- Range: ${impact.get('lower_bound_per_minute', 0):.2f}-${impact.get('upper_bound_per_minute', 0):.2f}/minute"]
    if record.get("stakeholder_updates"):
        lines += ["", "## Stakeholder Updates", "", *[f"- {role.title()}: {text}" for role, text in record.get("stakeholder_updates", {}).items() if text]]
    if record.get("troubleshooting_plan"):
        lines += ["", "## Troubleshooting Plan", "", *[f"- {step}" for step in record.get("troubleshooting_plan", [])]]
    if record.get("kpi_guardrails"):
        lines += ["", "## KPI Guardrails", "", *[f"- {item}" for item in record.get("kpi_guardrails", {}).get("operational_guardrails", []) or []], *[f"- {item}" for item in record.get("kpi_guardrails", {}).get("business_guardrails", []) or []]]
    if log_cache:
        lines += ["", "## Log Context", "", f"- Logs scanned: {log_cache.get('total_logs_scanned', 0):,}", f"- Error contexts cached: {len(log_cache.get('error_contexts', []))}"]
    lines += ["", "## Recovery Actions", "", *[f"{i + 1}. {rec} â€” _{decisions.get(str(i), {}).get('decision', 'pending review')}_" for i, rec in enumerate(remediation)]]
    if review_events:
        lines += ["", "## Human Approval Decisions", "", *[f"- {event.get('actor')} {event.get('action')} -> {event.get('decision')}: {event.get('reason', '')}" for event in review_events]]
    if lessons:
        lines += ["", "## Lessons Learned", "", *[f"- {item}" for item in lessons]]
    if preventive:
        lines += ["", "## Preventive Actions", "", *[f"- {item}" for item in preventive]]
    if reflections:
        lines += ["", "## Agent Reflections", "", *[f"- {item.get('agent')}: {item.get('reflection')} (confidence: {item.get('confidence', 0):.0f})" for item in reflections]]
    if record.get("similar_incidents"):
        lines += ["", "## Related Past Incidents", "", *[f"- Incident {s.get('incident_id') or s.get('number')} on {s.get('service')} ({str(s.get('resolved_at', ''))[:10]}): {s.get('hypothesis')} â€” {s.get('match_reason')}" for s in record["similar_incidents"]]]
    if timeline:
        lines += ["", "## Investigation Timeline", "", *[f"- `{str(inv.get('timestamp', ''))[11:19]}` **{inv.get('agent')}** â€” {inv.get('reasoning') or inv.get('hypothesis') or inv.get('action', '')}" for inv in timeline]]
    lines += ["", "---", "", "_Generated automatically by AI Operations Command Center_", ""]
    return "\n".join(lines)


def save_postmortem_markdown(record: Dict[str, Any]) -> Path:
    path = _postmortem_path(record)
    Path(POSTMORTEM_DIR).mkdir(parents=True, exist_ok=True)
    markdown = render_postmortem_markdown(record)
    path.write_text(markdown, encoding="utf-8")
    return path


def incident_memory_agent(record: Dict[str, Any]) -> None:
    """Persist incident memory as structured JSON and markdown postmortem."""
    record_incident(record)
    try:
        save_postmortem_markdown(record)
    except Exception:
        pass


def find_similar_incidents(state: IncidentState) -> List[Dict[str, Any]]:
    """Match the current incident against resolved past incidents by root
    cause and by service + anomaly-signature overlap. Returns the most
    recent matches first (max 3)."""
    hypothesis: str = (state.root_cause or {}).get("hypothesis", "")
    log_types = {a.get("type", "") for a in state.log_anomalies}

    matches: List[Dict[str, Any]] = []
    for past in _load():
        if past.get("incident_id") == state.incident_id:
            continue
        same_cause: bool = bool(hypothesis) and past.get("hypothesis") == hypothesis
        signature_overlap = log_types & set(past.get("log_anomaly_types", []))
        same_service: bool = past.get("service") == state.service
        if same_cause or (same_service and signature_overlap):
            match = dict(past)
            match["match_reason"] = (
                "same root cause"
                if same_cause
                else "same service with overlapping anomaly signature"
            )
            matches.append(match)
    return matches[::-1][:3]



def _extract_timeline(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for item in record.get("agent_invocations", []) or []:
        events.append({
            "time": item.get("timestamp") or item.get("started_at") or record.get("timestamp"),
            "actor": item.get("agent") or item.get("name") or "agent",
            "action": item.get("action") or item.get("status") or "observed",
            "finding": item.get("findings") or item.get("summary") or item.get("result"),
        })
    for item in record.get("review_events", []) or []:
        events.append({
            "time": item.get("decided_at") or item.get("timestamp") or record.get("timestamp"),
            "actor": item.get("actor") or "human-review",
            "action": item.get("action") or item.get("decision") or "review",
            "finding": item.get("reason") or item.get("new_value"),
        })
    if not events:
        events.append({"time": record.get("timestamp") or datetime.now().isoformat(), "actor": "system", "action": "incident_recorded", "finding": record.get("alert_description", "")})
    return events[:30]


def _failed_attempts(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    failed = []
    for key, value in (record.get("remediation_decisions") or {}).items():
        if value.get("decision") == "rejected":
            failed.append({"step_index": key, "reason": value.get("reason") or value.get("policy_reason") or "rejected during review"})
    for item in record.get("failed_attempts", []) or []:
        if isinstance(item, dict):
            failed.append(item)
        else:
            failed.append({"description": str(item)})
    return failed


def _lessons_learned(record: Dict[str, Any]) -> List[str]:
    lessons = list(record.get("lessons_learned") or [])
    rc = record.get("root_cause") or {}
    if rc.get("hypothesis"):
        lessons.append(f"Detect {rc.get('hypothesis')} earlier with service-specific alerts.")
    if record.get("similar_incidents"):
        lessons.append("Link new alerts to incident memory before manual triage to reduce MTTR.")
    if record.get("blast_radius", {}).get("estimated_scope") == "high":
        lessons.append("High blast-radius services need pre-approved rollback and escalation paths.")
    return list(dict.fromkeys(str(item) for item in lessons if item))[:8]


def _preventive_actions(record: Dict[str, Any]) -> List[str]:
    actions = list(record.get("preventive_actions") or [])
    for guardrail in (record.get("kpi_guardrails") or {}).get("preventive_actions", []) or []:
        actions.append(str(guardrail))
    for dep in record.get("dependencies", [])[:3]:
        actions.append(f"Add dependency health check and saturation alert for {dep}.")
    if not actions:
        actions.append("Create or update the service runbook with the validated remediation and rollback checks.")
    return list(dict.fromkeys(actions))[:8]


def _agent_reflections(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    reflections = list(record.get("agent_reflections") or [])
    if not reflections:
        confidence = float((record.get("root_cause") or {}).get("confidence") or 0)
        reflections.append({
            "agent": "Learning Agent",
            "reflection": "RCA confidence was sufficient for remediation planning." if confidence >= 0.7 else "Future incidents need more evidence before remediation planning.",
            "confidence": confidence,
        })
    return reflections[:8]


def record_incident(record: Dict[str, Any]) -> None:
    """Persist a completed incident so future investigations can cite it."""
    items: List[Dict[str, Any]] = _load()
    if any(p.get("incident_id") == record.get("incident_id") for p in items):
        return
    root_cause: Dict[str, Any] = record.get("root_cause") or {}
    items.append(
        {
            "number": len(items) + 1,
            "incident_id": record.get("incident_id"),
            "resolved_at": datetime.now().isoformat(),
            "service": record.get("service"),
            "severity": record.get("severity"),
            "hypothesis": root_cause.get("hypothesis", ""),
            "confidence": root_cause.get("confidence", 0),
            "alert_description": record.get("alert_description", ""),
            "current_status": record.get("current_status", "complete"),
            "affected_users": record.get("affected_users"),
            "estimated_revenue_impact_per_minute": record.get(
                "estimated_revenue_impact_per_minute"
            ),
            "estimated_cost_impact_per_minute": record.get(
                "estimated_cost_impact_per_minute"
            ),
            "revenue_impact_justification": record.get(
                "revenue_impact_justification", {}
            ),
            "log_anomaly_types": sorted(
                {a.get("type", "") for a in record.get("log_anomalies", [])}
            ),
            "recovery_recommendations": (record.get("recovery_recommendations") or [])[:5],
            "timeline": _extract_timeline(record),
            "successful_remediations": [
                rec for index, rec in enumerate(record.get("recovery_recommendations") or [])
                if (record.get("remediation_decisions") or {}).get(str(index), {}).get("decision") in {"approved", "executed"}
            ][:5],
            "failed_attempts": _failed_attempts(record),
            "lessons_learned": _lessons_learned(record),
            "preventive_actions": _preventive_actions(record),
            "agent_reflections": _agent_reflections(record),
            "contributing_factors": record.get("contributing_factors") or [],
            "rollbacks": record.get("rollback_plan") or {},
            "ownership": record.get("ownership") or {},
            "environment": record.get("environment") or {},
            "dependencies": record.get("dependencies") or [],
            "blast_radius": record.get("blast_radius") or {},
            "runbooks": record.get("runbooks") or [],
            "escalation_path": record.get("escalation_path") or [],
        }
    )
    _save(items)
