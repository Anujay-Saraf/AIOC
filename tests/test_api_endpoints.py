from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module
from agents import knowledge_base


def test_admin_route_serves_html() -> None:
    client = TestClient(app_module.app)
    response = client.get("/admin")

    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/html")
    assert response.text


def test_get_jarvis_status_includes_routing_and_provider(monkeypatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "test-token")
    client = TestClient(app_module.app)
    response = client.get("/api/jarvis/status", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    payload = response.json()

    assert isinstance(payload.get("routing"), dict)
    assert "provider" in payload
    assert "model" in payload
    # Runtime health fields were removed to keep the status response concise.


def test_upload_knowledge_endpoint_persists_and_searches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_base, "DATA_UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(knowledge_base, "KNOWLEDGE_DB_PATH", tmp_path / "knowledge_base.sqlite3")
    monkeypatch.setenv("API_AUTH_TOKEN", "test-token")
    knowledge_base.initialize_knowledge_base(str(tmp_path))

    with TestClient(app_module.app) as client:
        text = "This is a test upload document. It contains a cache stampede example."
        response = client.post(
            "/api/knowledge/upload",
            data={"text": text},
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["uploaded"] is True
        assert payload["chunk_id"].startswith("upload:")
        assert payload["source_path"].startswith(str(tmp_path / "uploads"))
        assert payload["title"].endswith(".txt")

        search_response = client.get(
            "/api/knowledge/search",
            params={"q": "cache stampede"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert search_response.status_code == 200
        search_payload = search_response.json()
        assert search_payload["query"] == "cache stampede"
        assert search_payload["results"]
        assert any("cache stampede" in result["content"].lower() for result in search_payload["results"])


def test_upload_log_for_analysis_creates_incident_and_saves_upload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_base, "DATA_UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(knowledge_base, "KNOWLEDGE_DB_PATH", tmp_path / "knowledge_base.sqlite3")
    monkeypatch.setenv("API_AUTH_TOKEN", "test-token")
    knowledge_base.initialize_knowledge_base(str(tmp_path))
    monkeypatch.setattr(app_module.asyncio, "create_task", lambda coro: None)

    app_module.incident_store.clear()
    app_module.incident_order.clear()

    def _close_coro(coro):
        try:
            coro.close()
        except Exception:
            pass

    monkeypatch.setattr(app_module.asyncio, "create_task", _close_coro)

    with TestClient(app_module.app) as client:
        log_text = (
            "2026-07-12T12:00:00Z ERROR Database connection pool exhausted due to too many clients\n"
            "2026-07-12T12:00:01Z ERROR Connection refused while attempting new database session\n"
        )
        response = client.post(
            "/api/incidents/upload-log",
            data={"text": log_text},
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["uploaded"] is True
        incident = payload["incident"]
        assert incident["service"] == "payment-api"
        assert incident["current_status"] == "investigating"
        assert incident["agent_status"] == "investigating"
        assert incident["lifecycle_status"] == "investigating"
        assert incident["incident_id"] in app_module.incident_store

    uploads = list((tmp_path / "uploads").glob("*.txt"))
    assert uploads
    assert any("pasted-log" in upload.name for upload in uploads)
    assert log_text.splitlines()[0] in uploads[0].read_text(encoding="utf-8")
