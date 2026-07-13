"""AIOC Incident Analysis Graph — Orchestration Engine.

Implements the 8-agent workflow specified in AIOC.md:

  Incident Commander → Log Analysis → Metrics Analysis → Deployment Analysis
  → Root Cause Analysis (with confidence retry loop)
  → Business Impact → Recovery Recommendations → Executive Summary
  → Human Approval Gate (HITL interrupt)
  → Learning & Memory

The graph is compiled with a SQLite checkpointer and interrupts BEFORE the
human_approval node so the graph can pause, persist its state, and resume
after a human makes an approve/reject decision via the API.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List

from langgraph.graph import END, StateGraph

from agents import IncidentState
from agents.business_impact import business_impact
from agents.debate import run_rca_debate
from agents.deployment_analysis import deployment_analysis
from agents.executive_summary import executive_summary
from agents.incident_commander import incident_commander
from agents.knowledge_base import build_knowledge_context
from agents.lifecycle import sync_lifecycle
from agents.llm import LLMUnavailableError, complete_json, get_model, llm_available
from agents.log_analysis import log_analysis
from agents.metrics_analysis import metrics_analysis
from agents.quality import evaluate_quality_gates
from agents.rca_agent import rca_analysis_with_llm
from agents.recovery_recommendations import recovery_recommendations
from agents.request_more_data_agent import request_more_data
from agents.router_agent import route_next_action_agentic, should_request_more_data
from agents.telemetry import record_invocation

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_compiled_graph: Any = None

SUMMARY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string"},
    },
    "required": ["executive_summary"],
    "additionalProperties": False,
}

# ─────────────────────────────────────────────────────────────────────────────
# Live state callback (used by app.py to stream updates to incident_store)
# ─────────────────────────────────────────────────────────────────────────────

# app.py registers a callback here before calling ainvoke:
#   set_node_callback(lambda state_dict: incident_store[id] = serialize(state_dict))
_node_callback: Any = None


def set_node_callback(fn: Any) -> None:
    """Register a function called after every graph node with the full state dict."""
    global _node_callback
    _node_callback = fn


def clear_node_callback() -> None:
    global _node_callback
    _node_callback = None


def _step_done(state: IncidentState, step: str) -> None:
    """Idempotently append a step name to completed_steps (List, not Set)."""
    if step not in state.completed_steps:
        state.completed_steps.append(step)


def _step_remove(state: IncidentState, step: str) -> None:
    """Remove a step from completed_steps (replaces Set.discard on a List)."""
    state.completed_steps = [s for s in state.completed_steps if s != step]


def _as_updates(state: IncidentState) -> Dict[str, Any]:
    """Snapshot state as a plain dict. Fires the live-update callback if set."""
    sync_lifecycle(state)
    state.quality_gates = evaluate_quality_gates(state)
    snapshot = dict(vars(state))
    if _node_callback is not None:
        try:
            _node_callback(snapshot)
        except Exception:
            pass  # Never crash the graph over a callback error
    return snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Graph nodes (1 per AIOC agent + routing infrastructure)
# ─────────────────────────────────────────────────────────────────────────────

async def _route_node(state: IncidentState) -> Dict[str, Any]:
    state.next_action = await route_next_action_agentic(state)
    return _as_updates(state)


def _select_next_node(state: IncidentState) -> str:
    return state.next_action


# ── AIOC Agent #1: Incident Commander ────────────────────────────────────────

def _incident_commander_node(state: IncidentState) -> Dict[str, Any]:
    """Receives the alert, triages it, and enriches context (ownership,
    dependencies, escalation path, runbooks, rollback plan)."""
    state = incident_commander(state)

    # Knowledge retrieval is folded into the commander phase so the
    # investigation agents have grounded context from the first step.
    query = f"{state.service}: {state.alert_description}"
    context = build_knowledge_context(query, incident_context=dict(vars(state)))
    state.retrieval_results = context.get("results", [])
    record_invocation(
        state,
        agent="knowledge_retrieval",
        action="retrieve_operational_knowledge",
        source="knowledge_base",
        reasoning="Retrieved operational runbooks and similar incident patterns for grounded investigation.",
        findings={
            "query": query,
            "result_count": len(state.retrieval_results),
            "retrieval_confidence": context.get("confidence"),
        },
    )

    _step_done(state, "incident_commander")
    state.current_status = "alert_triaged"
    state.lifecycle_status = "investigating"
    return _as_updates(state)


# ── AIOC Agent #2: Log Analysis ───────────────────────────────────────────────

def _analyze_logs_node(state: IncidentState) -> Dict[str, Any]:
    state = log_analysis(state)
    _step_done(state, "log_analysis")
    state.current_status = "logs_analyzed"
    return _as_updates(state)


# ── AIOC Agent #3: Metrics Analysis ──────────────────────────────────────────

def _analyze_metrics_node(state: IncidentState) -> Dict[str, Any]:
    state = metrics_analysis(state)
    _step_done(state, "metrics_analysis")
    state.current_status = "metrics_analyzed"
    return _as_updates(state)


# ── AIOC Agent #4: Deployment Analysis (NEW) ──────────────────────────────────

def _analyze_deployments_node(state: IncidentState) -> Dict[str, Any]:
    state = deployment_analysis(state)
    # _step_done already called inside deployment_analysis()
    return _as_updates(state)


# ── AIOC Agent #5: Root Cause Analysis (+ confidence retry loop) ─────────────

async def _run_rca_node(state: IncidentState) -> Dict[str, Any]:
    state = await rca_analysis_with_llm(state)
    state.current_status = "rca_completed"
    return _as_updates(state)


def _debate_rca_node(state: IncidentState) -> Dict[str, Any]:
    """Validates RCA evidence (debate/checklist). Deterministic — no LLM call."""
    state = run_rca_debate(state)
    return _as_updates(state)


def _request_more_data_node(state: IncidentState) -> Dict[str, Any]:
    state = request_more_data(state)
    return _as_updates(state)


# ── AIOC Agent #6: Business Impact ───────────────────────────────────────────

def _business_impact_node(state: IncidentState) -> Dict[str, Any]:
    state = business_impact(state)
    _step_done(state, "business_impact")
    state.current_status = "impact_calculated"
    return _as_updates(state)


# ── AIOC Agent #7: Recovery Recommendations (NEW) ────────────────────────────

async def _recovery_recommendations_node(state: IncidentState) -> Dict[str, Any]:
    state = await recovery_recommendations(state)
    # _step_done already called inside recovery_recommendations()
    return _as_updates(state)


# ── AIOC Agent #8: Executive Summary ─────────────────────────────────────────

async def _generate_summary_node(state: IncidentState) -> Dict[str, Any]:
    state = executive_summary(state)
    state = await _enhance_summary_with_llm(state)
    _step_done(state, "summary")
    state.current_status = "summary_ready"
    return _as_updates(state)


async def _enhance_summary_with_llm(state: IncidentState) -> IncidentState:
    """LLM rewrite of executive summary — narrative/communications focus only.
    Recovery recommendations are now owned by the Recovery Recommendation Agent."""
    if not llm_available():
        return state
    prompt = (
        "Write a concise incident report for company leadership based strictly on this completed analysis.\n\n"
        f"Service: {state.service}\n"
        f"Alert: {state.alert_description}\n"
        f"Severity: {state.severity}\n"
        f"Root cause: {(state.root_cause or {}).get('hypothesis', 'Under investigation')}\n"
        f"Confidence: {state.rca_confidence:.0%}\n"
        f"Affected users: {state.affected_users:,}\n"
        f"Revenue impact: ${state.estimated_revenue_impact_per_minute:.2f}/minute\n"
        f"Deployment correlation: {state.deployment_analysis.get('correlation_summary', 'Not analyzed')}\n"
        f"Recovery steps in progress: {state.recovery_recommendations[:3]}\n\n"
        "The executive_summary must be plain text (no markdown), at most 120 words, "
        "non-technical, and lead with business impact."
    )
    try:
        result: Dict[str, Any] = await complete_json(
            system="You are an incident communications specialist. Respond with JSON.",
            prompt=prompt,
            schema=SUMMARY_SCHEMA,
            schema_name="executive_summary",
        )
        if result.get("executive_summary"):
            state.executive_summary = result["executive_summary"]
        state.agent_invocations.append({
            "agent": "executive_summary",
            "timestamp": datetime.now().isoformat(),
            "action": "llm_enhance_summary",
            "source": f"llm:{get_model()}",
            "iteration": state.analysis_iterations,
        })
    except LLMUnavailableError as exc:
        # Primary + fallback providers both failed (quota/402/rate-limit/etc.).
        # Degrade gracefully: keep the deterministic executive summary and let
        # the graph node return normally instead of crashing the pipeline.
        logging.warning(
            "[summary] LLM enhancement unavailable (%s); keeping deterministic summary",
            exc,
        )
    except Exception as exc:
        logging.warning(
            "[summary] LLM enhancement failed (%s:%s); keeping deterministic summary",
            type(exc).__name__,
            exc,
        )
    return state


# ── Human Approval Gate (HITL — graph interrupts HERE) ───────────────────────

def _human_approval_node(state: IncidentState) -> Dict[str, Any]:
    """Marks the incident as needing human review.
    The graph is compiled with interrupt_before=['human_approval'] so
    LangGraph will pause BEFORE entering this node until the API resumes it.
    After resume, this node records the approval decision and continues."""
    high_risk_terms = ("rollback", "restart", "failover", "disable", "traffic", "scale", "drain")
    requires_approval = state.business_risk_level in {"critical", "high"} or any(
        term in str(rec).casefold()
        for rec in state.recovery_recommendations
        for term in high_risk_terms
    )
    state.remediation_policy = {
        "required": requires_approval,
        "risk_level": state.business_risk_level,
        "approval_reason": (
            "Human approval required — high-risk remediation actions detected."
            if requires_approval
            else "Standard remediation path — no explicit human approval required."
        ),
        "rollback_strategy": state.rollback_plan.get("strategy"),
        "safety_check": state.rollback_plan.get("safety_check"),
        "high_risk_steps": [
            s for s in (state.recovery_plan.get("steps") or [])
            if s.get("requires_approval")
        ],
    }
    state.lifecycle_status = "needs_human_review" if requires_approval else "investigating"
    _step_done(state, "human_approval")
    state.current_status = "approval_assessed"
    record_invocation(
        state,
        agent="human_approval_gate",
        action="assess_and_gate_remediation",
        source="policy",
        reasoning="Evaluated recovery actions for blast radius and marked high-risk steps for human approval.",
        findings=state.remediation_policy,
    )
    return _as_updates(state)


# ── Learning & Memory ─────────────────────────────────────────────────────────

def _learning_node(state: IncidentState) -> Dict[str, Any]:
    """Captures lessons learned and stores the incident for future memory matching."""
    reflection = {
        "agent": "learning_agent",
        "reflection": (
            "Investigation achieved high-confidence root cause with cited evidence."
            if state.rca_confidence >= 0.7
            else "Lower-confidence investigation — consider expanding data sources for similar future incidents."
        ),
        "confidence": state.rca_confidence,
        "deployment_correlation": state.deployment_analysis.get("correlation_summary", ""),
        "recovery_steps": len(state.recovery_recommendations),
    }
    state.agent_invocations.append({
        "agent": "learning_agent",
        "timestamp": datetime.now().isoformat(),
        "action": "capture_lessons_learned",
        "source": "heuristic",
        "reasoning": reflection["reflection"],
        "findings": reflection,
    })
    _step_done(state, "learning")
    state.current_status = "complete"
    state.lifecycle_status = "resolved"
    return _as_updates(state)


# ─────────────────────────────────────────────────────────────────────────────
# Graph assembly
# ─────────────────────────────────────────────────────────────────────────────

def create_incident_analysis_graph(checkpointer: Any = None) -> Any:
    """Build and compile the AIOC LangGraph.

    Args:
        checkpointer: Optional LangGraph checkpointer (e.g. AsyncSqliteSaver).
                      When provided, the graph supports HITL via interrupt_before.
    """
    graph: StateGraph = StateGraph(IncidentState)

    # Register nodes — one per AIOC agent
    graph.add_node("route_next_action", _route_node)
    graph.add_node("incident_commander", _incident_commander_node)    # AIOC Agent #1
    graph.add_node("analyze_logs", _analyze_logs_node)                # AIOC Agent #2
    graph.add_node("analyze_metrics", _analyze_metrics_node)          # AIOC Agent #3
    graph.add_node("analyze_deployments", _analyze_deployments_node)  # AIOC Agent #4 (NEW)
    graph.add_node("run_rca", _run_rca_node)                          # AIOC Agent #5
    graph.add_node("debate_rca", _debate_rca_node)                    # Validation layer
    graph.add_node("request_more_data", _request_more_data_node)      # Retry loop
    graph.add_node("business_impact", _business_impact_node)          # AIOC Agent #6
    graph.add_node("recovery_recommendations", _recovery_recommendations_node)  # AIOC Agent #7 (NEW)
    graph.add_node("generate_summary", _generate_summary_node)        # AIOC Agent #8
    graph.add_node("human_approval", _human_approval_node)            # HITL gate
    graph.add_node("learning", _learning_node)                        # Memory

    # Router dispatches to the correct next node based on completed_steps
    graph.add_conditional_edges(
        "route_next_action",
        _select_next_node,
        {
            "incident_commander": "incident_commander",
            "analyze_logs": "analyze_logs",
            "analyze_metrics": "analyze_metrics",
            "analyze_deployments": "analyze_deployments",
            "run_rca": "run_rca",
            "request_more_data": "request_more_data",
            "business_impact": "business_impact",
            "recovery_recommendations": "recovery_recommendations",
            "generate_summary": "generate_summary",
            "human_approval": "human_approval",
            "learning": "learning",
            "complete": END,
        },
    )

    # Sequential investigation nodes all return to the router
    for node in [
        "incident_commander",
        "analyze_logs",
        "analyze_metrics",
        "analyze_deployments",
        "business_impact",
        "recovery_recommendations",
        "generate_summary",
        "human_approval",
    ]:
        graph.add_edge(node, "route_next_action")

    # Confidence retry loop: RCA → validate → (retry if low confidence, else router)
    graph.add_edge("run_rca", "debate_rca")
    graph.add_conditional_edges(
        "debate_rca",
        should_request_more_data,
        {
            "low_confidence": "request_more_data",
            "high_confidence": "route_next_action",
        },
    )
    graph.add_edge("request_more_data", "route_next_action")

    # Terminal
    graph.add_edge("learning", END)
    graph.set_entry_point("route_next_action")

    # Compile: with checkpointer enables HITL resume; interrupt_before pauses graph
    if checkpointer is not None:
        return graph.compile(
            checkpointer=checkpointer,
            interrupt_before=["human_approval"],
        )
    return graph.compile()


def get_compiled_graph(checkpointer: Any = None) -> Any:
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = create_incident_analysis_graph(checkpointer=checkpointer)
    return _compiled_graph


async def run_incident_analysis(state: IncidentState) -> IncidentState:
    """Run the full graph (no checkpointer — for tests/scripting)."""
    graph: Any = get_compiled_graph()
    result: Any = await graph.ainvoke(
        dict(vars(state)), config={"recursion_limit": 80}
    )
    if isinstance(result, IncidentState):
        return result
    # Filter to only known fields to avoid errors from stale/extra fields in state
    known = set(IncidentState.__dataclass_fields__)
    return IncidentState(**{k: v for k, v in result.items() if k in known})

