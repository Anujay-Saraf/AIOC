from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents.knowledge_base import initialize_knowledge_base, search_knowledge
from agents.qa import answer_question
from agents import connector_registry
from app import _answer_incident_lookup, _matching_incident_records, incident_order, incident_store


def test_knowledge_search_returns_repo_docs() -> None:
    initialize_knowledge_base(".")
    hits = search_knowledge("incident response workflow", max_results=3)
    assert hits
    assert any("README.md" in hit.source_path or "WORKFLOW.md" in hit.source_path for hit in hits)


def test_demo_rag_ranks_cache_stampede_knowledge_by_intent() -> None:
    initialize_knowledge_base(".")
    fix_hits = search_knowledge("How do we fix a Redis cache stampede?", max_results=3)
    impact_hits = search_knowledge(
        "How many users and revenue were impacted in the search API incident?",
        max_results=3,
    )
    similar_hits = search_knowledge(
        "Is this similar to a traffic surge or database pool exhaustion?",
        max_results=3,
    )

    assert fix_hits[0].kind == "runbook"
    assert impact_hits[0].kind in {"postmortem", "service-profile"}
    assert similar_hits[0].kind == "similar-incident"


def test_incident_answer_can_cite_curated_runbook(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    initialize_knowledge_base(".")
    record = {
        "incident_id": "cache-demo",
        "service": "search-api",
        "alert_description": "Latency spike from cache stampede and retry amplification",
        "current_status": "complete",
        "root_cause": {"hypothesis": "Cache Stampede and Retry Amplification", "confidence": 0.83},
        "recovery_recommendations": ["Enable request coalescing and TTL jitter"],
    }

    payload, _ = asyncio.run(answer_question(record, "How do we fix a Redis cache stampede?"))

    assert any(citation.get("kind") == "runbook" for citation in payload["citations"])


def test_answer_question_returns_citations(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    payload, source = asyncio.run(answer_question({}, "What is the workflow?"))
    assert source == "heuristic"
    assert payload["answer"]
    assert payload["citations"]
    assert payload["follow_ups"]


def test_obsidian_vault_documents_are_indexed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(connector_registry, "CONNECTORS_PATH", tmp_path / "connectors.json")
    connector_registry.upsert_connector(
        {
            "type": "obsidian_vault",
            "name": "My Vault",
            "config": {
                "path": str(tmp_path / "vault"),
                "sync_mode": "manual",
            },
        }
    )
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True)
    note_path = vault_dir / "incident-runbook.md"
    note_path.write_text("# Incident Runbook\n\nThis note explains incident response best practices.", encoding="utf-8")

    initialize_knowledge_base(str(tmp_path))
    hits = search_knowledge("incident response", max_results=5)

    assert any("obsidian://" in hit.source_path for hit in hits)
    assert any("Incident Runbook" in hit.title for hit in hits)


def test_global_assistant_finds_live_db_pool_incident(monkeypatch) -> None:
    monkeypatch.setattr("app.list_incident_memory", lambda: [])
    incident_store.clear()
    incident_order.clear()
    incident_id = "db-pool-incident-1234"
    incident_store[incident_id] = {
        "incident_id": incident_id,
        "service": "payment-api",
        "alert_description": "Database connection pool exhaustion detected",
        "current_status": "complete",
        "affected_users": 1400,
        "estimated_revenue_impact_per_minute": 700.0,
        "root_cause": {
            "hypothesis": "Database Connection Pool Exhaustion",
            "confidence": 0.88,
        },
    }
    incident_order.append(incident_id)

    payload = _answer_incident_lookup(
        "can you show me the respective incidence occurred due to the DB pool exhaustion"
    )

    assert payload is not None
    assert "payment-api" in payload["answer"]
    assert incident_id in payload["answer"]
    assert payload["citations"][0]["kind"] == "incident"
    assert _matching_incident_records("impact of payment-api DB exhaustion")[0]["incident_id"] == incident_id

    incident_store.clear()
    incident_order.clear()


def test_global_assistant_finds_persisted_db_pool_incident(monkeypatch) -> None:
    incident_store.clear()
    incident_order.clear()
    monkeypatch.setattr(
        "app.list_incident_memory",
        lambda: [
            {
                "incident_id": "historical-db-1234",
                "service": "payment-api",
                "hypothesis": "Database connection pool exhaustion due to reduced pool size",
                "resolved_at": "2026-07-11T00:00:00",
            }
        ],
    )

    payload = _answer_incident_lookup("show incidents due to DB pool exhaustion")

    assert payload is not None
    assert "historical-db-1234" in payload["answer"]
    assert "Status: resolved" in payload["answer"]


def test_empty_record_does_not_invent_zero_impact(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    payload, _ = asyncio.run(answer_question({}, "How many users were affected?"))
    assert "cannot determine" in payload["answer"]
    assert "0 users" not in payload["answer"]


def test_empty_record_does_not_render_none_status(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    payload, _ = asyncio.run(answer_question({}, "Give me the status"))
    assert "cannot determine" in payload["answer"]
    assert "None" not in payload["answer"]
