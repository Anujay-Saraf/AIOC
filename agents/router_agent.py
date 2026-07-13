"""Router Agent — AIOC orchestration decision engine.

Determines the next investigation step based on what has been completed.
Provides two modes:
  1. Deterministic: ordered checklist (always available, zero latency)
  2. LLM-augmented: when multiple valid candidates exist, the LLM reasons
     about which to choose, constrained to the legal set (guardrail pattern)

KEY DESIGN: the router SKIPS agents that have no work to do. For each step a
``step_is_required`` guard decides whether the corresponding agent has
meaningful inputs (e.g. raw logs/metrics/deployments present). Steps whose
inputs are empty are treated as "done or skipped" and never block the flow.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from agents import IncidentState
from agents.llm import complete_json, get_model, llm_available

ROUTER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "reasoning"],
    "additionalProperties": False,
}

# Human-readable descriptions used in the LLM router prompt
ACTION_DESCRIPTIONS: Dict[str, str] = {
    "incident_commander": "Triage the alert, load service context (ownership, dependencies, runbooks, escalation path), and retrieve knowledge",
    "analyze_logs": "Scan raw application and infrastructure logs for anomalies, errors, and failure patterns",
    "analyze_metrics": "Compare incident-window metrics against baselines to detect operational degradation",
    "analyze_deployments": "Review recent deployments and config changes for temporal correlation with the incident",
    "run_rca": "Synthesize all evidence into a root cause hypothesis with calibrated confidence and cited evidence",
    "request_more_data": "Gather deeper evidence when RCA confidence is below the 0.7 threshold",
    "business_impact": "Translate the technical failure into affected users, revenue impact, and business risk level",
    "recovery_recommendations": "Generate prioritised, risk-tagged, approval-flagged recovery actions",
    "generate_summary": "Produce engineering and executive summaries of the completed investigation",
    "human_approval": "Gate high-risk recovery actions behind an explicit human approve/reject decision",
    "learning": "Persist the incident to memory and capture lessons learned for future incidents",
    "complete": "All investigation and reporting steps complete — close the incident",
}

# Canonical ordered list of steps. Each entry maps a step name to the
# completed_steps marker that records its completion.
STEP_ORDER: List[str] = [
    "incident_commander",
    "analyze_logs",
    "analyze_metrics",
    "analyze_deployments",
    "run_rca",
    "business_impact",
    "recovery_recommendations",
    "generate_summary",
    "human_approval",
    "learning",
    "complete",
]


def step_is_required(step: str, state: IncidentState) -> bool:
    """Return True if the given step must still be performed for this state.

    Agents with no relevant inputs are reported as not required, so the router
    transparently SKIPS them instead of blocking the investigation.
    """
    cs = state.completed_steps  # List[str]

    if step == "incident_commander":
        # Always the first step.
        return "incident_commander" not in cs

    if step == "analyze_logs":
        return bool(state.raw_logs) and "log_analysis" not in cs

    if step == "analyze_metrics":
        return bool(state.raw_metrics) and "metrics_analysis" not in cs

    if step == "analyze_deployments":
        return bool(state.deployment_changes) and "deployment_analysis" not in cs

    if step == "run_rca":
        # Only once the investigation steps are done (completed OR skipped).
        # The RCA agent marks completion with "rca_analysis" (not "run_rca").
        if "rca_analysis" in cs:
            return False
        return _investigation_complete(state)

    if step == "business_impact":
        return state.root_cause is not None and "business_impact" not in cs

    if step == "recovery_recommendations":
        return (
            bool(state.affected_users) and state.affected_users > 0
            and "recovery_recommendations" not in cs
        )

    if step == "generate_summary":
        # Terminal reporting step — always offered until completed.
        return "summary" not in cs

    if step == "human_approval":
        return (
            bool(state.remediation_policy)
            and state.remediation_policy.get("required") is True
            and "human_approval" not in cs
        )

    if step == "learning":
        # Memory/learning step runs after the summary is produced (and after any
        # required human approval). It persists the incident and sets the
        # terminal "complete" status in the learning node.
        return "summary" in cs and "learning" not in cs

    if step == "complete":
        # Terminal — only once the learning step has persisted the incident.
        return "learning" in cs

    return False


def _investigation_complete(state: IncidentState) -> bool:
    """True when all investigation agents are completed or skipped."""
    cs = state.completed_steps

    def _done_or_skipped(marker: str, step: str) -> bool:
        if marker in cs:
            return True
        return not step_is_required(step, state)

    return (
        _done_or_skipped("log_analysis", "analyze_logs")
        and _done_or_skipped("metrics_analysis", "analyze_metrics")
        and _done_or_skipped("deployment_analysis", "analyze_deployments")
    )


def valid_next_actions(state: IncidentState) -> List[str]:
    """Compute the ordered list of legal REQUIRED next actions for ``state``.

    PURE function — no LLM call. Returns only steps that ``step_is_required``
    marks as still needing work, so agents with no relevant inputs are skipped.
    """
    return [step for step in STEP_ORDER if step_is_required(step, state)]


async def route_next_action_agentic(state: IncidentState) -> str:
    """LLM-augmented router: reasons over the incident and selects next action.

    Constrained to ``valid_next_actions()`` — the LLM cannot pick anything
    outside the legal set. Falls back to deterministic when:
      - No LLM configured
      - Only one legal action (nothing to decide)
      - LLM returns an invalid action (guardrail override, logged)
    """
    candidates: List[str] = valid_next_actions(state)

    # Increment once — deterministic path does NOT increment again
    state.analysis_iterations += 1

    if not llm_available() or len(candidates) <= 1:
        decision: str = candidates[0]
        reasoning: str = (
            "Single valid required step — no LLM decision needed"
            if len(candidates) == 1
            else "Deterministic routing (no LLM configured)"
        )
        _record_routing(state, decision, reasoning, source="deterministic")
        return decision

    dep_summary = (state.deployment_analysis or {}).get("correlation_summary", "not yet analyzed")
    prompt: str = (
        "You are the orchestrator of a multi-agent incident response system.\n"
        "Decide the single best next investigation action.\n\n"
        f"Incident: {state.alert_description} on service '{state.service}' "
        f"(severity: {state.severity})\n"
        f"Completed steps: {state.completed_steps or 'none'}\n"
        f"Iteration: {state.analysis_iterations} of max {state.max_iterations}\n"
        f"RCA confidence: {state.rca_confidence:.2f}\n"
        f"Log anomalies: {len(state.log_anomalies)}\n"
        f"Metric anomalies: {len(state.metric_anomalies)}\n"
        f"Deployment correlation: {dep_summary}\n\n"
        "Valid actions right now:\n"
        + "\n".join(f"- {a}: {ACTION_DESCRIPTIONS.get(a, a)}" for a in candidates)
        + "\n\nChoose exactly one action from the valid list above and explain why in one sentence."
    )

    source = "deterministic"
    decision = candidates[0]
    reasoning = "Fallback"
    try:
        result: Dict[str, Any] = await complete_json(
            system="You are an autonomous SRE incident commander. Respond with JSON.",
            prompt=prompt,
            schema=ROUTER_SCHEMA,
            schema_name="routing_decision",
        )
        chosen = str(result.get("action", ""))
        reasoning = str(result.get("reasoning", ""))
        if chosen in candidates:
            decision = chosen
            source = f"llm:{get_model()}"
        else:
            print(f"[router] LLM chose invalid action '{chosen}'; guardrail selected '{candidates[0]}'")
            decision = candidates[0]
            reasoning = f"LLM chose invalid action '{chosen}'; guardrail override to '{decision}'"
            source = "guardrail"
    except Exception as exc:
        print(f"[router] LLM routing failed: {exc}")
        source = "deterministic"

    print(
        f"[router] iteration={state.analysis_iterations} "
        f"confidence={state.rca_confidence:.2f} decision={decision} ({source})"
    )
    _record_routing(state, decision, reasoning, source=source)
    return decision


def _record_routing(
    state: IncidentState, decision: str, reasoning: str, source: str
) -> None:
    state.agent_invocations.append({
        "agent": "router_agent",
        "timestamp": datetime.now().isoformat(),
        "action": f"route:{decision}",
        "source": source,
        "reasoning": reasoning,
        "iteration": state.analysis_iterations,
    })


def should_request_more_data(state: IncidentState) -> str:
    """Conditional edge after run_rca: route to retry loop if confidence < 0.7."""
    if state.rca_confidence < 0.7 and state.analysis_iterations < 3:
        return "low_confidence"
    return "high_confidence"


# Backwards-compatible alias for existing tests (sync deterministic routing)
def route_next_action(state: IncidentState) -> str:
    """Sync wrapper returning the next action respecting skip logic.

    Mirrors the decision path of ``route_next_action_agentic`` but without
    the LLM call. Used by tests and any deterministic routing.
    """
    actions = valid_next_actions(state)
    # Increment iteration so callers can detect activity
    state.analysis_iterations += 1
    decision = actions[0] if actions else "complete"
    _record_routing(state, decision, "sync route_next_action", source="deterministic")
    return decision
