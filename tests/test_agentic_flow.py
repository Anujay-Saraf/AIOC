"""Tests for the AIOC agentic workflow — router, graph, and end-to-end analysis.

Updated to match the AIOC v1.0 8-agent architecture:
  incident_commander → analyze_logs → analyze_metrics → analyze_deployments →
  run_rca → debate_rca → business_impact → recovery_recommendations →
  generate_summary → human_approval → learning
"""
import pytest

from agents import IncidentState
from agents.agentic_system import create_incident_analysis_graph, run_incident_analysis
from agents.request_more_data_agent import request_more_data
from agents.router_agent import route_next_action, should_request_more_data


def make_state(**overrides: object) -> IncidentState:
    defaults: dict = {
        "incident_id": "test-incident",
        "timestamp": "2026-07-07T14:32:15Z",
        "alert_description": "Test alert",
        "service": "payment-api",
        "severity": "critical",
    }
    defaults.update(overrides)
    return IncidentState(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# Router unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_router_decides_first_action() -> None:
    """Empty state → first step is always incident_commander."""
    state: IncidentState = make_state(incident_id="test1")
    action: str = route_next_action(state)
    assert action == "incident_commander"


def test_router_routes_to_logs_after_commander() -> None:
    state: IncidentState = make_state(
        incident_id="test2",
        completed_steps=["incident_commander"],
        raw_logs=[{"level": "ERROR", "message": "test error"}],
    )
    action: str = route_next_action(state)
    assert action == "analyze_logs"


def test_router_routes_to_metrics_after_logs() -> None:
    state: IncidentState = make_state(
        incident_id="test3",
        completed_steps=["incident_commander", "log_analysis"],
        raw_logs=[{"level": "ERROR", "message": "test error"}],
        raw_metrics=[{"metric": "requests_per_second", "value": 100}],
    )
    action: str = route_next_action(state)
    assert action == "analyze_metrics"


def test_router_routes_to_deployments_after_metrics() -> None:
    state: IncidentState = make_state(
        incident_id="test3b",
        completed_steps=["incident_commander", "log_analysis", "metrics_analysis"],
        raw_logs=[{"level": "ERROR", "message": "test error"}],
        raw_metrics=[{"metric": "requests_per_second", "value": 100}],
        deployment_changes=[{"version": "1.2.3", "timestamp": "2026-07-07T14:00:00Z"}],
    )
    action: str = route_next_action(state)
    assert action == "analyze_deployments"


def test_router_routes_to_rca_after_investigation() -> None:
    state: IncidentState = make_state(
        incident_id="test3c",
        completed_steps=["incident_commander", "log_analysis", "metrics_analysis", "deployment_analysis"],
    )
    action: str = route_next_action(state)
    assert action == "run_rca"


def test_router_routes_to_business_impact_after_rca() -> None:
    """After RCA + debate with high confidence, next is business_impact."""
    state: IncidentState = make_state(
        incident_id="test4",
        completed_steps=[
            "incident_commander", "log_analysis", "metrics_analysis",
            "deployment_analysis", "rca_analysis",
        ],
        raw_logs=[{"level": "ERROR", "message": "test error"}],
        raw_metrics=[{"metric": "requests_per_second", "value": 100}],
        deployment_changes=[{"version": "1.2.3", "timestamp": "2026-07-07T14:00:00Z"}],
        root_cause={"hypothesis": "test root cause", "confidence": 0.85},
        rca_confidence=0.85,
        analysis_iterations=2,
    )
    action: str = route_next_action(state)
    assert action == "business_impact"


def test_router_completes_after_all_steps() -> None:
    state: IncidentState = make_state(
        incident_id="test5",
        completed_steps=[
            "incident_commander", "log_analysis", "metrics_analysis",
            "deployment_analysis", "rca_analysis", "rca_debate",
            "business_impact", "recovery_recommendations",
            "summary", "human_approval", "learning",
        ],
        raw_logs=[{"level": "ERROR", "message": "test error"}],
        raw_metrics=[{"metric": "requests_per_second", "value": 100}],
        deployment_changes=[{"version": "1.2.3", "timestamp": "2026-07-07T14:00:00Z"}],
        root_cause={"hypothesis": "test root cause", "confidence": 0.85},
        affected_users=100,
        recovery_recommendations=["rollback"],
    )
    action: str = route_next_action(state)
    assert action == "complete"


def test_should_request_more_data_low_confidence() -> None:
    state: IncidentState = make_state(
        incident_id="test6",
        rca_confidence=0.62,
        analysis_iterations=2,
        max_iterations=5,
    )
    result: str = should_request_more_data(state)
    assert result == "low_confidence"


def test_should_request_more_data_high_confidence() -> None:
    state: IncidentState = make_state(
        incident_id="test7",
        rca_confidence=0.85,
        analysis_iterations=2,
    )
    result: str = should_request_more_data(state)
    assert result == "high_confidence"


def test_should_request_more_data_respects_max_iterations() -> None:
    state: IncidentState = make_state(
        incident_id="test8",
        rca_confidence=0.4,
        analysis_iterations=10,
        max_iterations=10,
    )
    result: str = should_request_more_data(state)
    assert result == "high_confidence"


def test_analysis_iterations_increment() -> None:
    state: IncidentState = make_state(
        incident_id="test9",
        completed_steps=["incident_commander"],
        raw_logs=[{"level": "ERROR", "message": "test error"}],
    )
    initial_iterations: int = state.analysis_iterations
    route_next_action(state)
    assert state.analysis_iterations > initial_iterations


def test_request_more_data_resets_rca_step() -> None:
    state: IncidentState = make_state(
        incident_id="test10",
        completed_steps=["incident_commander", "log_analysis", "metrics_analysis",
                         "deployment_analysis", "rca_analysis"],
    )
    state = request_more_data(state)
    assert "rca_analysis" not in state.completed_steps
    assert state.current_status == "requesting_deeper_analysis"
    assert any(
        inv["agent"] == "request_more_data_agent" for inv in state.agent_invocations
    )


# ─────────────────────────────────────────────────────────────────────────────
# Graph compilation tests
# ─────────────────────────────────────────────────────────────────────────────

def test_agentic_graph_compiles() -> None:
    graph: object = create_incident_analysis_graph()
    assert graph is not None


def test_graph_visualization_renders() -> None:
    graph: object = create_incident_analysis_graph()
    mermaid: str = graph.get_graph().draw_mermaid()
    assert "route_next_action" in mermaid
    assert "run_rca" in mermaid
    assert "debate_rca" in mermaid
    # New AIOC agents
    assert "analyze_deployments" in mermaid
    assert "recovery_recommendations" in mermaid
    assert "human_approval" in mermaid


def test_graph_has_all_aioc_nodes() -> None:
    """Verify all 8 AIOC agents are present as graph nodes."""
    graph: object = create_incident_analysis_graph()
    mermaid: str = graph.get_graph().draw_mermaid()
    expected_nodes = [
        "incident_commander",     # Agent #1
        "analyze_logs",           # Agent #2
        "analyze_metrics",        # Agent #3
        "analyze_deployments",    # Agent #4
        "run_rca",                # Agent #5
        "business_impact",        # Agent #6
        "recovery_recommendations",  # Agent #7
        "generate_summary",       # Agent #8
        "human_approval",         # HITL gate
        "learning",               # Memory
    ]
    for node in expected_nodes:
        assert node in mermaid, f"Expected node '{node}' not found in graph"


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end integration tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agentic_graph_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full end-to-end run without an LLM key — heuristic path throughout."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state: IncidentState = make_state(incident_id="test-e2e")
    result: IncidentState = await run_incident_analysis(state)

    assert result.root_cause is not None
    assert result.rca_confidence > 0.0
    assert result.affected_users >= 0
    assert result.current_status == "complete"
    assert "summary" in result.completed_steps
    assert "rca_debate" in result.completed_steps
    # New AIOC agents should have run
    assert "deployment_analysis" in result.completed_steps
    assert "recovery_recommendations" in result.completed_steps
    # human_approval is conditionally required (skipped when business risk is
    # low/unknown and no high-risk recovery action is present). In this
    # heuristic run with no risk signal it is correctly skipped.
    assert "human_approval" not in result.completed_steps
    assert "learning" in result.completed_steps
    # Deployment analysis should have output
    assert isinstance(result.deployment_analysis, dict)
    # Recovery plan should have steps
    assert len(result.recovery_recommendations) > 0


@pytest.mark.asyncio
async def test_agentic_graph_loops_on_low_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state: IncidentState = make_state(
        incident_id="test-loop",
        service="unknown-service",
    )
    result: IncidentState = await run_incident_analysis(state)
    assert result.current_status == "complete"
