from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from agents import connector_registry, llm, qa, query_memory


@pytest.fixture
def isolated_query_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPH_MEMORY_BACKEND", "memory")
    query_memory._MEMORY_ANSWERS.clear()
    query_memory._MEMORY_INCIDENTS.clear()
    monkeypatch.setattr(
        qa,
        "build_knowledge_context",
        lambda question, incident_context=None: {
            "query": question,
            "confidence": 0.88,
            "results": [
                {
                    "title": "Runbook",
                    "source_path": "runbook.md",
                    "kind": "runbook",
                    "content": "Restart only after checking pool pressure.",
                    "score": 0.88,
                    "citation": "runbook.md",
                }
            ],
        },
    )
    return None


def _local_result(answer: str = "Pool pressure caused the incident.") -> Dict[str, Any]:
    return {
        "answer": answer,
        "confidence": 0.91,
        "follow_ups": ["Check pool saturation?"],
        "language": "en",
        "citations": [{"source_path": "runbook.md"}],
        "answerable": True,
        "fallback_reason": "",
    }


def test_second_query_uses_persistent_memory_before_ollama(
    isolated_query_memory: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def local(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        nonlocal calls
        calls += 1
        return _local_result()

    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setattr(qa, "complete_local_json", local)
    monkeypatch.setattr(qa, "llm_available", lambda: False)
    record = {"incident_id": "inc-1", "service": "payments", "current_status": "investigating"}

    first, first_source = asyncio.run(qa.answer_question(record, "What caused the pool issue?"))
    second, second_source = asyncio.run(qa.answer_question(record, "What caused the pool issue?"))

    assert first_source == "llm:ollama/qwen2.5:1b"
    assert first["routing"]["tier"] == "local"
    assert second_source == "cache:graph-query-memory"
    assert second["routing"]["cache_hit"] is True
    assert second["routing"]["provider"] == "ollama"
    assert calls == 1
    assert query_memory.cache_stats()["backend"] == "in_memory_graph"


def test_context_change_invalidates_cache_key(
    isolated_query_memory: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def local(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        nonlocal calls
        calls += 1
        return _local_result()

    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setattr(qa, "complete_local_json", local)
    monkeypatch.setattr(qa, "llm_available", lambda: False)
    question = "What caused the pool issue?"
    asyncio.run(qa.answer_question({"incident_id": "inc-1", "current_status": "open"}, question))
    asyncio.run(qa.answer_question({"incident_id": "inc-1", "current_status": "resolved"}, question))

    assert calls == 2
    assert query_memory.cache_stats()["active_entries"] == 2


def test_low_confidence_local_answer_falls_back_to_selected_online_provider(
    isolated_query_memory: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def local(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        result = _local_result()
        result.update(answerable=False, confidence=0.31, fallback_reason="insufficient graph evidence")
        return result

    async def online(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {
            "answer": "The online fallback found pool exhaustion.",
            "confidence": 0.87,
            "follow_ups": [],
            "language": "en",
            "citations": [{"source_path": "runbook.md"}],
        }

    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setattr(qa, "complete_local_json", local)
    monkeypatch.setattr(qa, "llm_available", lambda: True)
    monkeypatch.setattr(qa, "complete_json", online)
    monkeypatch.setattr(qa, "get_provider", lambda: "groq")
    monkeypatch.setattr(qa, "get_model", lambda: "fallback-model")

    payload, source = asyncio.run(qa.answer_question({}, "Explain the pool issue"))

    assert source == "llm:groq/fallback-model"
    assert payload["routing"]["tier"] == "online"
    assert payload["routing"]["provider"] == "groq"
    assert "ollama_not_answerable" in payload["routing"]["fallback_reason"]


def test_global_query_receives_bounded_system_snapshot(
    isolated_query_memory: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = ""

    async def local(system: str, prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal captured
        captured = prompt
        return _local_result("There is one active payment incident.")

    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setattr(qa, "complete_local_json", local)
    monkeypatch.setattr(qa, "llm_available", lambda: False)
    snapshot = {
        "incidents": [{"incident_id": "inc-1", "service": "payments"}],
        "connectors": [{"name": "Logs", "status": "online"}],
    }
    payload, _ = asyncio.run(
        qa.answer_question({}, "What is happening in the system?", system_context=snapshot)
    )

    assert "inc-1" in captured
    assert "Logs" in captured
    assert payload["routing"]["tier"] == "local"


def test_admin_provider_connector_selects_groq(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connector_registry, "CONNECTORS_PATH", tmp_path / "connectors.json")
    monkeypatch.setenv("TEST_GROQ_KEY", "secret")
    connector_registry.upsert_connector(
        {
            "type": "llm_groq",
            "name": "Primary Groq",
            "config": {
                "api_key_env": "TEST_GROQ_KEY",
                "model": "test-model",
                "active": "true",
            },
        }
    )

    config = llm.get_online_config()

    assert config is not None
    assert config.provider == "groq"
    assert config.model == "test-model"
    assert config.api_key == "secret"
    assert config.source.startswith("connector:")


def test_catalog_includes_obsidian_and_all_online_providers() -> None:
    types = {item["type"] for item in connector_registry.CONNECTOR_CATALOG}
    assert "obsidian_vault" in types
    assert {"llm_openai", "llm_gemini", "llm_groq", "llm_claude"} <= types


def test_cache_can_be_invalidated(isolated_query_memory: None) -> None:
    fingerprint = query_memory.context_fingerprint({}, {"results": []})
    stored = query_memory.remember_answer(
        "What is the status?",
        fingerprint,
        "en",
        {"answer": "Stable", "confidence": 0.9},
        provider="ollama",
        model="qwen2.5:1b",
    )

    assert stored is True
    assert query_memory.cache_stats()["active_entries"] == 1
    assert query_memory.invalidate_query_memory(fingerprint=fingerprint) == 1
    assert query_memory.cache_stats()["active_entries"] == 0
