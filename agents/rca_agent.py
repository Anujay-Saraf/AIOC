from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List

from agents import IncidentState
from agents.confidence import compute_confidence
from agents.context_compaction import build_compact_context
from agents.evidence import (
    evidence_refs_from_state,
    attach_evidence_ids,
    build_evidence_catalog,
)
from agents.llm import complete_json, get_model, llm_available
from agents.memory import find_similar_incidents
from agents.knowledge_base import kg_query
from agents.rca_analysis import rca_analysis
from agents.telemetry import Timer, record_invocation
from agents.connectors import get_telemetry_provider
from agents.log_analysis import log_analysis


# ---------------------------------------------------------------------------
# Inline debate logic (originally in agents/debate.py)
# ---------------------------------------------------------------------------
def _run_rca_debate(state: IncidentState) -> IncidentState:
    max_rounds = max(1, min(int(os.getenv("RCA_DEBATE_MAX_ROUNDS", "2")), 3))
    if len(state.debate_rounds) >= max_rounds:
        if "rca_debate" not in state.completed_steps:
            state.completed_steps.append("rca_debate")
        state.current_status = "rca_debated"
        return state

    timer = Timer.begin()
    root_cause = state.root_cause or {}
    proposer_span = state.current_parent_span_id
    round_number = len(state.debate_rounds) + 1
    exchanges: List[Dict[str, Any]] = []

    evidence = list(root_cause.get("supporting_evidence") or [])
    evidence_refs = list(root_cause.get("supporting_evidence_refs") or [])
    alternatives = list(root_cause.get("ruled_out_hypotheses") or [])
    evidence_challenges: List[str] = []
    if len(evidence) < 3:
        evidence_challenges.append("Fewer than three supporting evidence claims were supplied")
    if len(evidence_refs) < min(3, len(evidence)):
        evidence_challenges.append("Some RCA claims do not resolve to raw evidence IDs")
    if len(alternatives) < 2:
        evidence_challenges.append("Fewer than two plausible alternatives were ruled out")
    if not evidence_challenges:
        evidence_challenges.append("Evidence coverage and alternative elimination are sufficient")
    evidence_verdict = "challenge" if any("Fewer" in item or "do not" in item for item in evidence_challenges) else "support"
    evidence_span = record_invocation(
        state,
        agent="evidence_critic",
        action="critique_rca_evidence",
        source="deterministic_debate",
        reasoning="; ".join(evidence_challenges),
        findings={"verdict": evidence_verdict, "challenges": evidence_challenges},
        parent_span_id=proposer_span,
        extra={"debate_round": round_number, "debate_role": "critic"},
    )
    exchanges.append({"agent": "evidence_critic", "verdict": evidence_verdict, "points": evidence_challenges})

    critic_spans = [evidence_span["span_id"]]
    needs_operations_critic = (
        state.severity.lower() == "critical"
        or state.rca_confidence < 0.85
        or bool(state.deployment_changes)
    )
    if needs_operations_critic:
        ops_points = _operations_critique(state)
        ops_verdict = "challenge" if any("missing" in point.lower() for point in ops_points) else "support"
        ops_span = record_invocation(
            state,
            agent="operations_critic",
            action="critique_operational_safety",
            source="deterministic_debate",
            reasoning="; ".join(ops_points),
            findings={"verdict": ops_verdict, "challenges": ops_points},
            parent_span_id=proposer_span,
            extra={"debate_round": round_number, "debate_role": "critic"},
        )
        critic_spans.append(ops_span["span_id"])
        exchanges.append({"agent": "operations_critic", "verdict": ops_verdict, "points": ops_points})

    challenges = [item for item in exchanges if item["verdict"] == "challenge"]
    if challenges:
        original_confidence = float(root_cause.get("confidence", state.rca_confidence) or 0.0)
        revised_confidence = max(0.0, round(original_confidence - min(0.12, 0.04 * len(challenges)), 3))
        root_cause["confidence"] = revised_confidence
        state.rca_confidence = revised_confidence
        record_invocation(
            state,
            agent="rca_reviser",
            action="revise_rca_after_critique",
            source="deterministic_debate",
            reasoning="Reduced confidence until critic challenges receive additional evidence.",
            findings={"before": original_confidence, "after": revised_confidence},
            parent_span_id=proposer_span,
            extra={"debate_round": round_number, "debate_role": "reviser"},
        )

    decision = "accepted" if not challenges else "accepted_with_caveats"
    judge = record_invocation(
        state,
        agent="debate_judge",
        action="adjudicate_rca_debate",
        source="deterministic_debate",
        reasoning=(
            "Accepted the RCA because both critics found adequate grounding and operational safety."
            if not challenges
            else "Accepted provisionally with explicit confidence reduction and critic caveats."
        ),
        findings={
            "decision": decision,
            "critic_spans": critic_spans,
            "challenge_count": len(challenges),
            "final_confidence": state.rca_confidence,
        },
        parent_span_id=proposer_span,
        latency_ms=timer.ms(),
        extra={"debate_round": round_number, "debate_role": "judge"},
    )
    debate_round = {
        "round": round_number,
        "proposer_span_id": proposer_span,
        "critic_span_ids": critic_spans,
        "judge_span_id": judge["span_id"],
        "decision": decision,
        "exchanges": exchanges,
        "final_confidence": state.rca_confidence,
    }
    state.debate_rounds.append(debate_round)
    root_cause["debate"] = list(state.debate_rounds)
    state.root_cause = root_cause
    if "rca_debate" not in state.completed_steps:
        state.completed_steps.append("rca_debate")
    state.current_status = "rca_debated"
    return state


def _operations_critique(state: IncidentState) -> List[str]:
    """Inline operations critique extracted from agents/debate.py."""
    points: List[str] = []
    hypothesis = str((state.root_cause or {}).get("hypothesis", "")).lower()
    if state.deployment_changes and not (state.root_cause or {}).get("deploy_correlation"):
        points.append("Deployment correlation is missing despite a recent change")
    else:
        points.append("Deployment correlation was explicitly considered")
    if "cache" in hypothesis and not any(
        anomaly.get("type") == "retry_storm" for anomaly in state.log_anomalies
    ):
        points.append("Retry-storm evidence is missing for the cache-stampede hypothesis")
    else:
        points.append("The proposed failure mode matches the observed operational signals")
    return points


# ---------------------------------------------------------------------------
# Inline request-more-data logic (originally in request_more_data_agent.py)
# ---------------------------------------------------------------------------
def _request_more_data(state: IncidentState) -> IncidentState:
    """Widen the data window: fetch logs/deployments, re-run log analysis."""
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


RCA_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "confidence": {"type": "number"},
        "supporting_evidence": {"type": "array", "items": {"type": "string"}},
        "ruled_out_hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["hypothesis", "reason"],
                "additionalProperties": False,
            },
        },
        "deploy_correlation": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": [
        "hypothesis",
        "confidence",
        "supporting_evidence",
        "ruled_out_hypotheses",
        "deploy_correlation",
        "reasoning",
    ],
    "additionalProperties": False,
}


async def rca_analysis_with_llm(state: IncidentState) -> IncidentState:
    """Root cause analysis that merges the Debate and Request-More-Data agents.

    Runs the LLM analysis to populate ``state.root_cause`` / ``state.rca_confidence``,
    applies a LOCAL deterministic evidence debate (no extra LLM call), and — when
    confidence is below 0.7 and iterations remain — widens the data window and
    re-runs the analysis. Bounded to at most 3 total passes.
    """
    max_retries = 3

    while True:
        # --- LLM analysis step -------------------------------------------------
        t = Timer.begin()
        result: Dict[str, Any] = {}
        source: str = "heuristic_fallback"
        llm_self_report: float | None = None

        if llm_available():
            compact = build_compact_context(state)
            state.compact_contexts.append(compact.manifest)
            prompt: str = (
                "You are a senior site reliability engineer performing root cause "
                "analysis on a production incident.\n\n"
                "You MUST ground every claim in the provided JSON context. If evidence "
                "is ambiguous, say so and lower confidence.\n\n"
                "Treat traffic surges, retry storms, cost spikes, and dependency saturation "
                "as valid failure families when the evidence supports them.\n\n"
                f"CONTEXT_JSON:\n{compact.as_json()}\n\n"
                "Determine the most likely root cause. Return:\n"
                "- hypothesis: a short root-cause title\n"
                "- confidence: calibrated between 0 and 1\n"
                "- supporting_evidence: 3-5 items, each citing an EXACT log message snippet, "
                "metric name with baseline->current values, or deployment change from the JSON. "
                "Never cite evidence not present.\n"
                "- ruled_out_hypotheses: 2 plausible alternative causes you considered "
                "and dismissed, each with the specific JSON data point that rules it out\n"
                "- deploy_correlation: if a deployment timestamp precedes the incident "
                "start, one sentence linking it; empty string if none\n"
                "- reasoning: one sentence describing how you weighed the evidence"
            )
            try:
                result = await complete_json(
                    system="You are an expert SRE. Ground every claim in the provided evidence.",
                    prompt=prompt,
                    schema=RCA_SCHEMA,
                    schema_name="root_cause_analysis",
                )
                source = f"llm:{get_model()}"
            except Exception as exc:
                print(f"[rca] LLM call failed, using heuristic fallback: {exc}")
                result = {}

        if result:
            llm_self_report = min(max(float(result.get("confidence", 0.5)), 0.0), 1.0)
            breakdown = compute_confidence(state, llm_self_report=llm_self_report)
            supporting_evidence = result.get("supporting_evidence", [])
            supporting_evidence_refs = evidence_refs_from_state(state, supporting_evidence)
            state.root_cause = {
                "hypothesis": result.get("hypothesis", "Unknown"),
                "confidence": breakdown.score,
                "supporting_evidence": supporting_evidence,
                "supporting_evidence_refs": supporting_evidence_refs,
                "ruled_out_hypotheses": result.get("ruled_out_hypotheses", []),
                "deploy_correlation": result.get("deploy_correlation", ""),
                "reasoning": result.get("reasoning", ""),
                "confidence_breakdown": {
                    "evidence_strength": breakdown.evidence_strength,
                    "signal_count": breakdown.signal_count,
                    "deploy_correlation": breakdown.deploy_correlation,
                    "signal_diversity": breakdown.signal_diversity,
                    "anomaly_severity": breakdown.anomaly_severity,
                    "data_completeness": breakdown.data_completeness,
                    "alternatives_ruled_out": breakdown.alternatives_ruled_out,
                    "historical_similarity": breakdown.historical_similarity,
                    "llm_self_report": breakdown.llm_self_report,
                },
            }
        else:
            state = rca_analysis(state)

        state.rca_confidence = float(state.root_cause.get("confidence", 0.0))
        if "rca_analysis" not in state.completed_steps:
            state.completed_steps.append("rca_analysis")

        # --- Deterministic debate (no extra LLM call) --------------------------
        state = _run_rca_debate(state)

        # --- Record this pass --------------------------------------------------
        record_invocation(
            state,
            agent="rca_agent",
            action="run_rca",
            source=source,
            reasoning=(
                result.get("reasoning", "")
                or state.root_cause.get("reasoning", "")
                or f"Pattern-matched evidence against known failure signatures; best fit '{state.root_cause.get('hypothesis')}'"
            ),
            findings={
                "hypothesis": state.root_cause.get("hypothesis"),
                "confidence": state.rca_confidence,
                "confidence_breakdown": state.root_cause.get("confidence_breakdown", {}),
            },
            output_refs={
                "supporting_evidence_count": len(state.root_cause.get("supporting_evidence") or []),
            },
            latency_ms=t.ms(),
        )
        state.agent_invocations.append(
            {
                "agent": "rca_agent",
                "timestamp": datetime.now().isoformat(),
                "action": "rca_iteration",
                "iteration": state.analysis_iterations,
                "confidence": state.rca_confidence,
            }
        )

        # --- Confidence gate: stop or widen the data window --------------------
        if state.rca_confidence >= 0.7 or state.analysis_iterations >= max_retries - 1:
            break
        state.analysis_iterations += 1
        state = _request_more_data(state)
        # loop re-runs the LLM analysis with the expanded data

    # --- Final pass: build evidence citations ---------------------------------
    evidence = state.root_cause.get("supporting_evidence", []) if isinstance(state.root_cause, dict) else []
    refs = state.root_cause.get("supporting_evidence_refs", []) if isinstance(state.root_cause, dict) else []
    citations: List[Dict[str, Any]] = []
    for claim, src in zip(evidence, refs):
        citations.append({"claim": claim, "source": src, "confidence": state.rca_confidence})

    # --- Non-blocking knowledge-graph enrichment of the RCA hypothesis --------
    # Pull cited facts about root causes of similar past incidents so the
    # hypothesis carries historical grounding/citations in state.evidence_citations.
    try:
        hypothesis = (state.root_cause or {}).get("hypothesis", "")
        kg_result = kg_query(
            f"root cause for {state.service} {hypothesis or state.alert_description}",
            incident_context={
                "service": state.service,
                "alert_description": state.alert_description,
                "root_cause": state.root_cause,
                "incident_id": state.incident_id,
            },
        )
        for fact in kg_result.get("facts", []):
            citations.append({
                "claim": fact["fact"],
                "source": fact["citation"],
                "confidence": fact["confidence"],
                "via": "knowledge_graph",
            })
    except Exception:
        pass
    state.evidence_citations = citations

    # --- Memory recall (unchanged) --------------------------------------------
    state.similar_incidents = find_similar_incidents(state)
    if state.similar_incidents:
        top: Dict[str, Any] = state.similar_incidents[0]
        state.agent_invocations.append(
            {
                "agent": "memory",
                "timestamp": datetime.now().isoformat(),
                "action": "recall_similar_incidents",
                "source": "memory",
                "reasoning": (
                    f"This matches incident #{top.get('number')} on {top.get('service')} "
                    f"from {str(top.get('resolved_at', ''))[:10]} — same pattern: "
                    f"{top.get('hypothesis')} ({top.get('match_reason')})"
                ),
                "iteration": state.analysis_iterations,
            }
        )

    return state
