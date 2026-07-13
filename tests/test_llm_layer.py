import pytest

from agents import IncidentState
from agents.llm import get_model, get_provider, llm_available
from agents.router_agent import route_next_action_agentic, valid_next_actions


def make_state(**overrides: object) -> IncidentState:
    defaults: dict = {
        "incident_id": "test-llm",
        "timestamp": "2026-07-08T10:00:00Z",
        "alert_description": "Test alert",
        "service": "payment-api",
        "severity": "critical",
    }
    defaults.update(overrides)
    return IncidentState(**defaults)


def test_no_provider_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert get_provider() is None
    assert not llm_available()
    assert get_model() == "heuristic"


def test_openai_provider_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert get_provider() == "openai"
    assert llm_available()


def test_valid_actions_initial_state() -> None:
    state: IncidentState = make_state()
    assert valid_next_actions(state) == ["incident_commander"]


def test_valid_actions_after_load() -> None:
    state: IncidentState = make_state(completed_steps={"incident_commander"})
    actions: list = valid_next_actions(state)
    assert actions == ["analyze_logs"]


def test_valid_actions_low_confidence_loop() -> None:
    state: IncidentState = make_state(
        completed_steps={
            "incident_commander",
            "log_analysis",
            "metrics_analysis",
            "deployment_analysis",
            "rca_analysis",
            "rca_debate",
        },
        rca_confidence=0.5,
        analysis_iterations=2,
    )
    assert valid_next_actions(state) == ["request_more_data"]


def test_valid_actions_complete() -> None:
    state: IncidentState = make_state(
        completed_steps={
            "incident_commander",
            "log_analysis",
            "metrics_analysis",
            "deployment_analysis",
            "rca_analysis",
            "rca_debate",
            "business_impact",
            "recovery_recommendations",
            "summary",
            "human_approval",
            "learning",
        },
        rca_confidence=0.9,
    )
    assert valid_next_actions(state) == ["complete"]


@pytest.mark.asyncio
async def test_agentic_router_falls_back_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state: IncidentState = make_state()
    decision: str = await route_next_action_agentic(state)
    assert decision == "incident_commander"
    assert any(inv["agent"] == "router_agent" for inv in state.agent_invocations)
