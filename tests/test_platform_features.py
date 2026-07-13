from datetime import datetime, timezone

from agents.analytics import incident_analytics, knowledge_graph
from agents import connector_registry as connectors
from agents import memory as incident_memory
from agents import query_memory
from app import _is_high_impact_record


def sample(incident_id: str) -> dict:
    return {
        "incident_id": incident_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "service": "payments",
        "current_status": "complete",
        "lifecycle_status": "resolved",
        "alert_description": "Database pool exhausted",
        "root_cause": {"hypothesis": "Database pool exhausted"},
        "estimated_revenue_impact_per_minute": 50,
    }


def test_analytics_buckets_recurring_incidents() -> None:
    result = incident_analytics([sample("one"), sample("two")], "week")
    assert result["total"] == 2
    assert result["recurring"][0]["count"] == 2
    assert result["impact_per_minute"] == 100


def test_knowledge_graph_connects_operational_entities() -> None:
    result = knowledge_graph([sample("one")])
    assert {node["type"] for node in result["nodes"]} == {"incident", "service", "cause", "resolution"}
    assert {edge["relation"] for edge in result["edges"]} == {"affects", "caused_by", "resolved_by"}


def test_connector_registry_masks_secret_fields(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(connectors, "CONNECTORS_PATH", tmp_path / "connectors.json")
    saved = connectors.upsert_connector({"type": "mcp", "name": "Ops MCP", "config": {"endpoint": "https://example.test/mcp", "token": "secret"}})
    assert saved["config"]["token"] == "********"
    assert connectors.list_connectors()[0]["name"] == "Ops MCP"


def test_catalog_includes_memgraph_and_teams_connectors() -> None:
    types = {item["type"] for item in connectors.CONNECTOR_CATALOG}
    assert {"memgraph", "teams"} <= types


def test_graph_memory_records_incident_properties(monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_MEMORY_BACKEND", "memory")
    query_memory._MEMORY_INCIDENTS.clear()
    query_memory._MEMORY_EVIDENCE.clear()
    record = sample("graph-one")
    record["current_status"] = "investigating"
    record["estimated_revenue_impact_per_minute"] = 2500

    assert query_memory.upsert_incident_graph(record) is True
    graph = query_memory.incident_graph_snapshot([])
    incident = next(node for node in graph["nodes"] if node["id"] == "graph-one")

    assert graph["backend"] == "in_memory_graph"
    assert incident["status"] == "investigating"
    assert incident["impact_per_minute"] == 2500


def test_graph_memory_links_documented_evidence_to_incident(monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_MEMORY_BACKEND", "memory")
    query_memory._MEMORY_INCIDENTS.clear()
    query_memory._MEMORY_EVIDENCE.clear()
    record = sample("graph-doc")
    knowledge = {
        "results": [
            {
                "chunk_id": "runbook:checkout:001",
                "title": "Checkout recovery runbook",
                "source_path": "runbooks/checkout.md",
                "kind": "runbook",
                "content": "Check gateway authorization retries before approving failover.",
                "score": 0.93,
                "citation": "runbooks/checkout.md",
                "tags": ["runbook", "checkout"],
            }
        ]
    }

    stored = query_memory.upsert_knowledge_evidence("Should we approve failover?", knowledge, record=record)
    graph = query_memory.incident_graph_snapshot([])

    assert stored == 1
    assert any(node["type"] == "evidence" and node.get("chunk_id") == "runbook:checkout:001" for node in graph["nodes"])
    assert any(node["type"] == "evidence" and node.get("vector_source") == "qdrant" for node in graph["nodes"])
    assert any(edge["source"] == "graph-doc" and edge["relation"] == "grounded_by" for edge in graph["edges"])


def test_graph_memory_preserves_semantic_chunk_ids_for_knowledge_chunks(monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_MEMORY_BACKEND", "memory")
    query_memory._MEMORY_INCIDENTS.clear()
    query_memory._MEMORY_EVIDENCE.clear()
    record = sample("graph-kgs")
    chunk = query_memory.KnowledgeChunk(
        chunk_id="qdrant:semantic:1234",
        title="Deployment history summary",
        source_path="data/knowledge/deployments.md",
        kind="deployment-summary",
        content="Recent deployment history indicates a config change for payment-api.",
        updated_at=datetime.now(timezone.utc).isoformat(),
        tags="deployment,config,service",
    )

    query_memory.upsert_incident_graph(record)
    stored = query_memory.upsert_knowledge_chunks_as_graph_nodes([chunk], record=record)
    graph = query_memory.incident_graph_snapshot([])

    assert stored == 1
    evidence_node = next(node for node in graph["nodes"] if node["type"] == "evidence")
    assert evidence_node["chunk_id"] == "qdrant:semantic:1234"
    assert evidence_node["vector_source"] == "qdrant"
    assert evidence_node["source_path"] == "data/knowledge/deployments.md"
    assert any(edge["source"] == "graph-kgs" and edge["relation"] == "grounded_by" for edge in graph["edges"])


def test_batch_operational_knowledge_updates_graph_and_vector_store(monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_MEMORY_BACKEND", "memory")
    monkeypatch.setenv("QDRANT_URL", "")
    query_memory._MEMORY_INCIDENTS.clear()
    query_memory._MEMORY_EVIDENCE.clear()
    record = sample("graph-batch")
    record["deployment_history"] = [
        {"timestamp": "2026-07-10T12:00:00Z", "version": "v1.2.3", "source": "ci/cd", "changes": ["Updated payment API config to reduce timeout."]}
    ]
    record["configuration_changes"] = [
        {"timestamp": "2026-07-10T11:45:00Z", "source": "feature-flag", "description": "Toggled auth retry backoff."}
    ]
    record["runbooks"] = ["data/knowledge/payment-db-pool-runbook.md"]
    record["business_criticality"] = "critical"
    record["related_incidents"] = ["incident-previous-001"]

    stored = query_memory.upsert_operational_incident_knowledge(record)
    graph = query_memory.incident_graph_snapshot([])

    assert stored >= 1
    assert any(node["type"] == "evidence" and node.get("kind") == "deployment_history" for node in graph["nodes"])
    assert any(node["type"] == "evidence" and node.get("kind") == "configuration_change" for node in graph["nodes"])
    assert any(node["type"] == "evidence" and node.get("kind") == "runbook_reference" for node in graph["nodes"])
    assert any(node["type"] == "evidence" and node.get("kind") == "business_criticality" for node in graph["nodes"])
    assert any(edge["source"] == "graph-batch" and edge["relation"] == "related_to" for edge in graph["edges"])
    assert any(edge["source"] == "graph-batch" and edge["relation"] == "grounded_by" for edge in graph["edges"])


def test_high_impact_alert_decision_uses_intake_marker_and_threshold(monkeypatch) -> None:
    marked, reason = _is_high_impact_record({"high_impact": True, "severity": "critical"})
    assert marked is True
    assert "intake" in reason

    monkeypatch.setenv("HIGH_IMPACT_ALERT_THRESHOLD_PER_MINUTE", "500")
    threshold_hit, threshold_reason = _is_high_impact_record(
        {"severity": "critical", "estimated_revenue_impact_per_minute": 700}
    )
    assert threshold_hit is True
    assert "exceeds" in threshold_reason


def test_incident_memory_agent_writes_json_and_markdown(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(incident_memory, "MEMORY_PATH", str(tmp_path / "incident_memory.json"))
    monkeypatch.setattr(incident_memory, "POSTMORTEM_DIR", str(tmp_path / "postmortems"))

    record = sample("memory-md-001")
    record["trace_id"] = "trace-memory-md-001"
    record["timestamp"] = datetime.now(timezone.utc).isoformat()
    record["executive_summary"] = "The service outage was caused by an unexpected database pool exhaustion."
    record["root_cause"] = {"hypothesis": "Database pool exhaustion", "confidence": 0.92}
    record["recovery_recommendations"] = ["Scale DB connection pool and improve query efficiency."]

    incident_memory.incident_memory_agent(record)

    persisted = incident_memory.list_incident_memory()
    assert len(persisted) == 1
    assert persisted[0]["incident_id"] == "memory-md-001"

    postmortem_file = tmp_path / "postmortems" / "postmortem-memory-md-001.md"
    assert postmortem_file.exists()
    content = postmortem_file.read_text(encoding="utf-8")
    assert "# Incident Postmortem" in content
    assert "Database pool exhaustion" in content
    assert "Scale DB connection pool" in content
