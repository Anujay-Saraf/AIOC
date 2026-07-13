import asyncio
import random
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

load_dotenv()

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "").rstrip("/") or PUBLIC_BASE_URL
WEB_INTERNAL_URL = os.getenv("WEB_INTERNAL_URL", "http://127.0.0.1:3000").rstrip("/")

from agents import IncidentState
from agents.agentic_system import create_incident_analysis_graph, get_compiled_graph, set_node_callback, clear_node_callback
from agents.authz import authorize_review, require_api_access, evaluate_remediation_policy
from agents.analytics import incident_analytics, knowledge_graph
from agents.connector_registry import (
    CONNECTOR_CATALOG,
    delete_connector,
    heartbeat,
    list_connectors,
    upsert_connector,
    runtime_connectors,
)
from agents.lifecycle import append_review_event, set_lifecycle_after_review
from agents.llm import complete_json, get_model, get_provider, llm_available, ollama_health, public_routing_config
from agents.knowledge_base import build_knowledge_context, initialize_knowledge_base, insert_uploaded_document, search_knowledge
from agents.memory import list_incident_memory, incident_memory_agent
from agents.notify import post_high_risk_email_alert, post_teams_alert, post_teams_handshake, post_war_room, teams_configured, teams_runtime_config, war_room_configured
from agents.qa import answer_question
from agents.query_memory import (
    cache_stats,
    incident_graph_snapshot,
    invalidate_query_memory,
    upsert_incident_graph,
    upsert_operational_incident_knowledge,
    upsert_knowledge_evidence,
)
from agents.context_builder import build_incident_context, enrich_record, SERVICE_CATALOG
from agents.quality import evaluate_quality_gates
from agents.slack_assistant import (
    SlackAssistant,
    incident_blocks,
    parse_command,
    scenario_from_text,
    summarize_incident,
)

app: FastAPI = FastAPI(title="AI Operations Command Center")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_PROXY_SKIP_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# In-memory demo pipeline health store (dummy Datadog/Splunk simulation)
pipeline_store: Dict[str, Dict[str, Any]] = {
    "datadog": {"name": "datadog", "status": "unknown", "last_checked": None, "latency_ms": None, "error_rate": None, "message": "not checked"},
    "splunk": {"name": "splunk", "status": "unknown", "last_checked": None, "latency_ms": None, "error_rate": None, "message": "not checked"},
}

_pipeline_simulator_task: Any = None
_pipeline_alerted: Dict[str, bool] = {}


def _command_center_html(path: str = "") -> str:
    ui_path = Path("frontend") / "command_center.html"
    try:
        return ui_path.read_text(encoding="utf-8")
    except Exception:
        return "<html><body><h1>AI Incident Command Center</h1><p>Fallback UI file missing.</p></body></html>"


def _web_redirect_url(path: str = "", query: str = "") -> str:
    base = WEB_INTERNAL_URL or WEB_BASE_URL or PUBLIC_BASE_URL
    target = base.rstrip("/")
    if path:
        target = f"{target}/{path.lstrip('/')}"
    else:
        target = f"{target}/"
    if query:
        target = f"{target}?{query}"
    return target


async def _proxy_web(request: Request, path: str) -> Response:
    target_path = f"/{path}" if path else "/"
    target_url = f"{WEB_INTERNAL_URL}{target_path}"
    body = await request.body()
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=3.0) as client:
            upstream = await client.request(
                request.method,
                target_url,
                params=request.query_params or None,
                content=body if body else None,
                headers={
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() not in {"host", "content-length"}
                },
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={
                key: value
                for key, value in upstream.headers.items()
                if key.lower() not in _PROXY_SKIP_HEADERS
            },
            media_type=upstream.headers.get("content-type"),
        )
    except Exception:
        return Response(content=_command_center_html(path), media_type="text/html")

incident_store: Dict[str, Dict[str, Any]] = {}
incident_order: List[str] = []
reinvestigation_jobs: Dict[str, Dict[str, Any]] = {}
REINVESTIGATION_JOBS_PATH = os.path.join("data", "reinvestigation_jobs.json")
slack_assistant = SlackAssistant()



def _synthetic_incidents() -> List[Dict[str, Any]]:
    """Demo-safe incidents used when live data is too small for dashboard analytics."""
    base = [
        ("synthetic-day-001", "payment-api", "critical", "Database pool exhaustion caused checkout failures", "Connection pool exhausted after deploy increased DB concurrency", 1840, 1280.0, 0),
        ("synthetic-day-002", "search-api", "high", "Cache stampede increased search latency", "Redis cache stampede with retry amplification", 620, 210.0, 0),
        ("synthetic-week-003", "order-processor", "high", "Memory leak caused worker restarts", "JVM heap leak in order enrichment worker", 410, 95.0, 2),
        ("synthetic-week-004", "checkout-gateway", "critical", "Downstream timeout cascade", "Retry storm against payment-api and tax-api", 2200, 1640.0, 5),
        ("synthetic-month-005", "catalog-api", "medium", "Catalog 500s after schema migration", "Backward-incompatible catalog DB migration", 300, 75.0, 14),
        ("synthetic-month-006", "payment-api", "high", "Repeated DB pool saturation", "Connection pool exhaustion recurring signature", 900, 580.0, 21),
    ]
    from datetime import timedelta, timezone
    now = datetime.now(timezone.utc)
    records: List[Dict[str, Any]] = []
    for iid, service, severity, alert, cause, users, impact, age in base:
        record = {
            "incident_id": iid,
            "trace_id": iid,
            "timestamp": (now - timedelta(days=age)).isoformat(),
            "created_at": (now - timedelta(days=age)).isoformat(),
            "service": service,
            "severity": severity,
            "alert_description": alert,
            "current_status": "complete",
            "lifecycle_status": "resolved",
            "root_cause": {"hypothesis": cause, "confidence": 0.86},
            "affected_users": users,
            "estimated_revenue_impact_per_minute": impact,
            "recovery_recommendations": [
                "Apply the service runbook remediation and validate KPIs.",
                "Keep human approval in the loop before rollback or traffic shifts.",
            ],
            "agent_invocations": _default_agent_journey(service, cause),
            "synthetic": True,
        }
        enrich_record(record)
        record["context_metadata"] = build_incident_context(record)
        records.append(record)
    return records


def _analytics_records(include_synthetic: bool = True) -> List[Dict[str, Any]]:
    live = [incident_store[item] for item in incident_order if item in incident_store]
    live_ids = {str(item.get("incident_id")) for item in live}
    persisted = [item for item in reversed(list_incident_memory()) if str(item.get("incident_id")) not in live_ids]
    records = [*live, *persisted]
    if include_synthetic and len(records) < 4:
        records = [*records, *_synthetic_incidents()]
    for record in records:
        enrich_record(record)
    return records


def _analytics_records_with_history() -> List[Dict[str, Any]]:
    """Return live + persisted records enriched with a rich historical baseline.

    The baseline covers the past 30 days with varied services, severities, and
    resolutions so that day / week / month analytics always show meaningful variation
    even when all live incidents were created today (demo / fresh restart scenario).
    """
    from datetime import datetime, timedelta, timezone as _tz

    live = [incident_store[item] for item in incident_order if item in incident_store]
    live_ids = {str(item.get("incident_id")) for item in live}
    persisted = [item for item in reversed(list_incident_memory()) if str(item.get("incident_id")) not in live_ids]
    for record in [*live, *persisted]:
        enrich_record(record)

    # Historical baseline — one rich record per age bucket so period filters differ
    now = datetime.now(_tz.utc)

    def _ts(days_ago: float, hours: float = 0) -> str:
        return (now - timedelta(days=days_ago, hours=hours)).isoformat()

    BASELINE = [
        # --- TODAY (age < 1 day) ---
        {"incident_id": "hist-001", "service": "payment-api", "severity": "critical",
         "alert_description": "DB connection pool exhausted — payment processing failures",
         "current_status": "complete", "root_cause": {"hypothesis": "Connection pool size misconfigured after deploy v2.4.1", "confidence": 0.91},
         "affected_users": 12400, "estimated_revenue_impact_per_minute": 640.0,
         "lifecycle_status": "resolved", "created_at": _ts(0.1),
         "recovery_recommendations": [
             "Revert JDBC pool config to pre-deploy v2.4.1 values (max_pool_size=50 → 200)",
             "Enable connection pool metrics alerting in Datadog: db.pool.waitTime > 500ms",
             "Run db-connection-pool-validator.sh to confirm pool saturation cleared",
             "Apply HikariCP idle-timeout tuning to prevent future misconfiguration",
             "Post-incident: add pool-size guardrails to deploy pipeline pre-check",
         ],
         "rollback_plan": {"safety_check": "payment 5xx_rate < 0.1% and checkout_latency_p99 < 400ms"},
         "completed_steps": ["incident_commander", "log_analysis", "metrics_analysis", "rca_analysis", "summary"]},
        {"incident_id": "hist-002", "service": "checkout-gateway", "severity": "high",
         "alert_description": "Retry storm from checkout-gateway amplifying payment-api load",
         "current_status": "complete", "root_cause": {"hypothesis": "Aggressive retry with no backoff on upstream failure", "confidence": 0.88},
         "affected_users": 8200, "estimated_revenue_impact_per_minute": 520.0,
         "lifecycle_status": "resolved", "created_at": _ts(0.3),
         "recovery_recommendations": [
             "Enable exponential backoff (base 500ms, max 30s, jitter 20%) on payment-api retries",
             "Set circuit breaker threshold: trip after 5 consecutive 5xx within 10s window",
             "Reduce retry max-attempts from 10 → 3 to limit amplification",
             "Add load-shedding queue at checkout-gateway with 2s timeout SLA",
         ],
         "rollback_plan": {"safety_check": "payment-api RPS < 8000/s and checkout 5xx_rate < 0.5%"},
         "completed_steps": ["incident_commander", "log_analysis", "metrics_analysis", "rca_analysis", "summary"]},
        # --- YESTERDAY (1-2 days ago) ---
        {"incident_id": "hist-003", "service": "search-api", "severity": "high",
         "alert_description": "Cache stampede — Elasticsearch overloaded after Redis cluster failover",
         "current_status": "complete", "root_cause": {"hypothesis": "Redis cluster failover caused cache miss storm hitting Elasticsearch directly", "confidence": 0.85},
         "affected_users": 31000, "estimated_revenue_impact_per_minute": 310.0,
         "lifecycle_status": "resolved", "created_at": _ts(1.2),
         "recovery_recommendations": [
             "Enable probabilistic early expiration (PER) on search-cache to stagger expiry",
             "Add Redis Sentinel health check to circuit-break Elasticsearch direct calls",
             "Increase Elasticsearch query queue depth from 200 → 1000 during failover events",
             "Deploy cache-warming job to pre-populate top-1000 search queries post-failover",
         ],
         "rollback_plan": {"safety_check": "search p99 latency < 800ms and Elasticsearch CPU < 70%"},
         "completed_steps": ["incident_commander", "log_analysis", "metrics_analysis", "rca_analysis", "summary"]},
        {"incident_id": "hist-004", "service": "order-processor", "severity": "critical",
         "alert_description": "Kafka consumer lag 68k messages — DLQ overflow",
         "current_status": "complete", "root_cause": {"hypothesis": "payment-api cascade caused consumer thread exhaustion and OOMKill", "confidence": 0.87},
         "affected_users": 5800, "estimated_revenue_impact_per_minute": 180.0,
         "lifecycle_status": "resolved", "created_at": _ts(1.8),
         "recovery_recommendations": [
             "Scale order-processor replicas from 3 → 8 immediately to drain backlog",
             "Reprocess DLQ messages from timestamp of first OOMKill (priority order: high-value orders first)",
             "Add JVM heap limit (-Xmx4g) and memory alert at 85% threshold",
             "Implement consumer-lag SLO alert: trip at lag > 5000 messages",
         ],
         "rollback_plan": {"safety_check": "Kafka consumer lag < 500 messages and DLQ size = 0"},
         "completed_steps": ["incident_commander", "log_analysis", "metrics_analysis", "rca_analysis", "summary"]},
        # --- 3 DAYS AGO ---
        {"incident_id": "hist-005", "service": "catalog-api", "severity": "medium",
         "alert_description": "Product image CDN latency spike — p99 > 4s",
         "current_status": "complete", "root_cause": {"hypothesis": "CDN edge node misconfiguration in eu-west-1 after certificate rotation", "confidence": 0.79},
         "affected_users": 4100, "estimated_revenue_impact_per_minute": 68.0,
         "lifecycle_status": "resolved", "created_at": _ts(3.1),
         "recovery_recommendations": [
             "Re-issue CDN TLS cert for eu-west-1 edge nodes via Certificate Manager",
             "Purge stale CDN cache for /images/* in eu-west-1",
             "Add CDN health probe monitoring with 30s interval",
         ],
         "rollback_plan": {"safety_check": "CDN p99 latency < 300ms for 5 consecutive minutes"},
         "completed_steps": ["incident_commander", "log_analysis", "rca_analysis", "summary"]},
        # --- 5 DAYS AGO ---
        {"incident_id": "hist-006", "service": "payment-api", "severity": "critical",
         "alert_description": "GC pause > 4s — heap fragmentation after memory leak in PaymentSessionCache",
         "current_status": "complete", "root_cause": {"hypothesis": "PaymentSessionCache TTL reduced from 5m to 30s increased heap churn", "confidence": 0.92},
         "affected_users": 9600, "estimated_revenue_impact_per_minute": 590.0,
         "lifecycle_status": "resolved", "created_at": _ts(5.0),
         "recovery_recommendations": [
             "Revert PaymentSessionCache TTL from 30s → 5min (config: session.cache.ttl=300)",
             "Force full GC cycle via JMX: ManagementFactory.getMemoryMXBean().gc()",
             "Enable G1GC with -XX:MaxGCPauseMillis=200 to bound pause time",
             "Add heap fragmentation alert at -XX:G1HeapFragmentation > 15%",
             "Review all cache TTL config changes in deployment pipeline",
         ],
         "rollback_plan": {"safety_check": "JVM GC pause_time < 500ms and heap usage < 70%"},
         "completed_steps": ["incident_commander", "log_analysis", "metrics_analysis", "rca_analysis", "business_impact", "summary"]},
        {"incident_id": "hist-007", "service": "fraud-service", "severity": "medium",
         "alert_description": "Fraud score model timeout — bypassing checks for 12 min",
         "current_status": "complete", "root_cause": {"hypothesis": "ML model inference timeout due to cold start after deployment", "confidence": 0.81},
         "affected_users": 2200, "estimated_revenue_impact_per_minute": 42.0,
         "lifecycle_status": "resolved", "created_at": _ts(5.5),
         "recovery_recommendations": [
             "Enable model warm-up endpoint: POST /model/warmup before traffic routing",
             "Add readiness probe: check /model/health returns 200 before k8s pod accepts traffic",
             "Set inference timeout from 2s → 5s during first 3 minutes post-deploy",
         ],
         "rollback_plan": {"safety_check": "fraud-service model p99 inference < 800ms"},
         "completed_steps": ["incident_commander", "log_analysis", "rca_analysis", "summary"]},
        # --- 8 DAYS AGO ---
        {"incident_id": "hist-008", "service": "notification-service", "severity": "low",
         "alert_description": "Email delivery failure — SES throttling exceeded daily quota",
         "current_status": "complete", "root_cause": {"hypothesis": "Batch notification job consumed full SES quota before transactional emails", "confidence": 0.94},
         "affected_users": 840, "estimated_revenue_impact_per_minute": 12.0,
         "lifecycle_status": "resolved", "created_at": _ts(8.2),
         "recovery_recommendations": [
             "Implement SES quota reservation: hold 40% quota for transactional emails",
             "Throttle batch notification jobs to 20% of remaining daily quota",
             "Add SES quota alert at 60% daily consumption",
         ],
         "rollback_plan": {"safety_check": "transactional email delivery rate > 99% for 10 min"},
         "completed_steps": ["incident_commander", "log_analysis", "rca_analysis", "summary"]},
        {"incident_id": "hist-009", "service": "search-api", "severity": "high",
         "alert_description": "Elasticsearch shard rebalancing — query latency p99 > 8s",
         "current_status": "complete", "root_cause": {"hypothesis": "Elasticsearch hot shard imbalance triggered automatic rebalancing during peak traffic", "confidence": 0.83},
         "affected_users": 18400, "estimated_revenue_impact_per_minute": 190.0,
         "lifecycle_status": "resolved", "created_at": _ts(8.9),
         "recovery_recommendations": [
             "Pause automatic shard rebalancing during peak hours (08:00-22:00 UTC)",
             "Redistribute shards to underloaded nodes: POST /_cluster/reroute with allocate commands",
             "Enable shard allocation awareness by zone to prevent hot shard clustering",
             "Set cluster.routing.allocation.cluster_concurrent_rebalance=2 to rate-limit rebalancing",
         ],
         "rollback_plan": {"safety_check": "Elasticsearch search p99 < 500ms and shard health = green"},
         "completed_steps": ["incident_commander", "log_analysis", "metrics_analysis", "rca_analysis", "summary"]},
        # --- 12 DAYS AGO ---
        {"incident_id": "hist-010", "service": "checkout-gateway", "severity": "critical",
         "alert_description": "Tax service timeout — all checkout requests failing at tax calculation step",
         "current_status": "complete", "root_cause": {"hypothesis": "Tax service deployment v1.9.2 introduced blocking DB call without connection pool limit", "confidence": 0.89},
         "affected_users": 22000, "estimated_revenue_impact_per_minute": 1240.0,
         "lifecycle_status": "resolved", "created_at": _ts(12.0),
         "recovery_recommendations": [
             "Rollback tax-service to v1.9.1 immediately via: kubectl rollout undo deploy/tax-service",
             "Add HikariCP connection pool (max=50, timeout=3s) to the blocking DB call in v1.9.2",
             "Add circuit breaker for tax calculation with fallback to cached tax rate",
             "Gate future tax-service deploys on DB query benchmarks < 100ms p99",
         ],
         "rollback_plan": {"safety_check": "checkout 5xx_rate < 0.1% and tax calculation latency p99 < 200ms"},
         "completed_steps": ["incident_commander", "log_analysis", "metrics_analysis", "rca_analysis", "business_impact", "summary"]},
        {"incident_id": "hist-011", "service": "catalog-api", "severity": "medium",
         "alert_description": "Inventory cache sync delay — stale stock counts shown to users",
         "current_status": "complete", "root_cause": {"hypothesis": "Redis eviction policy change evicted inventory cache keys under memory pressure", "confidence": 0.77},
         "affected_users": 3300, "estimated_revenue_impact_per_minute": 55.0,
         "lifecycle_status": "resolved", "created_at": _ts(12.6),
         "recovery_recommendations": [
             "Revert Redis eviction policy from allkeys-lru → volatile-lru to protect inventory keys",
             "Add inventory cache keys with TTL=0 (no-eviction) via Redis PERSIST command",
             "Increase Redis maxmemory from 4GB → 6GB to reduce eviction pressure",
         ],
         "rollback_plan": {"safety_check": "inventory cache hit ratio > 95% for 5 consecutive minutes"},
         "completed_steps": ["incident_commander", "log_analysis", "rca_analysis", "summary"]},
        # --- 16 DAYS AGO ---
        {"incident_id": "hist-012", "service": "payment-api", "severity": "high",
         "alert_description": "Fraud check circuit breaker open — elevated fraudulent charge rate",
         "current_status": "complete", "root_cause": {"hypothesis": "fraud-service SLA breach opened circuit breaker, bypassing fraud checks for 23 min", "confidence": 0.86},
         "affected_users": 6800, "estimated_revenue_impact_per_minute": 430.0,
         "lifecycle_status": "resolved", "created_at": _ts(16.0),
         "recovery_recommendations": [
             "Force-close circuit breaker and re-enable fraud checks: PUT /circuit-breaker/fraud/close",
             "Replay all 23 min of bypassed transactions through fraud-service in offline mode",
             "Set fraud-service SLO alert: p99 latency > 1.5s triggers PagerDuty",
             "Add circuit-breaker half-open mode with 1% traffic sampling before full re-enable",
         ],
         "rollback_plan": {"safety_check": "fraud_check_bypass_rate = 0% and fraud-service p99 < 1s"},
         "completed_steps": ["incident_commander", "log_analysis", "metrics_analysis", "rca_analysis", "summary"]},
        {"incident_id": "hist-013", "service": "order-processor", "severity": "medium",
         "alert_description": "Order enrichment timeout — 2100 orders stuck in pending state",
         "current_status": "complete", "root_cause": {"hypothesis": "inventory-service response time regression in v3.1.0 caused enrichment timeouts", "confidence": 0.80},
         "affected_users": 2100, "estimated_revenue_impact_per_minute": 88.0,
         "lifecycle_status": "resolved", "created_at": _ts(16.8),
         "recovery_recommendations": [
             "Rollback inventory-service to v3.0.9: kubectl rollout undo deploy/inventory-service",
             "Reprocess 2100 stuck orders via: POST /admin/reprocess-pending?before=<incident_ts>",
             "Set enrichment timeout from 2s → 5s with exponential backoff",
         ],
         "rollback_plan": {"safety_check": "order enrichment p99 < 1s and pending_count = 0"},
         "completed_steps": ["incident_commander", "log_analysis", "rca_analysis", "summary"]},
        # --- 21 DAYS AGO ---
        {"incident_id": "hist-014", "service": "search-api", "severity": "critical",
         "alert_description": "OOMKill loop — search-api pods restarting every 90 seconds",
         "current_status": "complete", "root_cause": {"hypothesis": "Thundering herd on pod restart: cache cold start caused immediate memory pressure", "confidence": 0.90},
         "affected_users": 42000, "estimated_revenue_impact_per_minute": 380.0,
         "lifecycle_status": "resolved", "created_at": _ts(21.0),
         "recovery_recommendations": [
             "Add pod startup delay (initialDelaySeconds=30) so cache warm-up completes before traffic",
             "Increase memory limit from 2Gi → 4Gi to accommodate cold-start cache loading",
             "Enable Redis pre-warming via cache-warmer sidecar on pod start",
             "Add PodDisruptionBudget: minAvailable=2 to prevent all pods restarting simultaneously",
         ],
         "rollback_plan": {"safety_check": "pod restart count = 0 for 10 min and memory_usage < 75%"},
         "completed_steps": ["incident_commander", "log_analysis", "metrics_analysis", "rca_analysis", "business_impact", "summary"]},
        {"incident_id": "hist-015", "service": "fraud-service", "severity": "high",
         "alert_description": "ML feature store unavailable — model falling back to rule-based scoring",
         "current_status": "complete", "root_cause": {"hypothesis": "Feature store Kafka consumer lost offset after broker leadership change", "confidence": 0.83},
         "affected_users": 1800, "estimated_revenue_impact_per_minute": 35.0,
         "lifecycle_status": "resolved", "created_at": _ts(21.5),
         "recovery_recommendations": [
             "Reset Kafka consumer offset to last known good position: kafka-consumer-groups --reset-offsets",
             "Restart feature-store consumer pod to re-subscribe after broker leadership stabilizes",
             "Add consumer offset lag alert: trip at lag > 10000 messages",
         ],
         "rollback_plan": {"safety_check": "feature store consumer lag < 100 and model_inference = ml-based"},
         "completed_steps": ["incident_commander", "log_analysis", "rca_analysis", "summary"]},
        # --- 26 DAYS AGO ---
        {"incident_id": "hist-016", "service": "checkout-gateway", "severity": "high",
         "alert_description": "Apple Pay integration timeout — mobile checkout conversion drop 38%",
         "current_status": "complete", "root_cause": {"hypothesis": "Apple Pay PKI certificate expired, causing all Apple Pay token validation to fail", "confidence": 0.96},
         "affected_users": 14200, "estimated_revenue_impact_per_minute": 720.0,
         "lifecycle_status": "resolved", "created_at": _ts(26.0),
         "recovery_recommendations": [
             "Renew Apple Pay PKI cert via Apple Developer Portal (expires in cert_expiry_date field)",
             "Deploy new cert to checkout-gateway as k8s Secret and trigger rolling restart",
             "Add cert-expiry monitoring alert: alert at 30, 14, 7 days before expiry",
             "Add Apple Pay /validate endpoint health probe to k8s liveness check",
         ],
         "rollback_plan": {"safety_check": "Apple Pay token_validation_success_rate > 99% for 5 min"},
         "completed_steps": ["incident_commander", "log_analysis", "rca_analysis", "summary"]},
        {"incident_id": "hist-017", "service": "payment-api", "severity": "medium",
         "alert_description": "Refund processing delayed — async refund queue backed up 4 hours",
         "current_status": "complete", "root_cause": {"hypothesis": "Worker thread pool reduced in config change lowered refund throughput by 80%", "confidence": 0.88},
         "affected_users": 1200, "estimated_revenue_impact_per_minute": 18.0,
         "lifecycle_status": "resolved", "created_at": _ts(26.5),
         "recovery_recommendations": [
             "Revert worker thread pool size from 5 → 25 in payment-api-config.yaml",
             "Trigger expedited reprocessing of queued refunds: POST /admin/refund-queue/flush",
             "Add thread pool saturation alert: alert when queue depth > 500 pending refunds",
         ],
         "rollback_plan": {"safety_check": "refund queue depth = 0 and refund p99 processing time < 3min"},
         "completed_steps": ["incident_commander", "log_analysis", "rca_analysis", "summary"]},
        {"incident_id": "hist-018", "service": "notification-service", "severity": "low",
         "alert_description": "Push notification delivery p99 > 30s — user complaints about delayed order confirmations",
         "current_status": "complete", "root_cause": {"hypothesis": "FCM quota exceeded for high-volume promotional campaign batch", "confidence": 0.91},
         "affected_users": 620, "estimated_revenue_impact_per_minute": 8.0,
         "lifecycle_status": "resolved", "created_at": _ts(27.0),
         "recovery_recommendations": [
             "Pause promotional campaign batch job immediately",
             "Reserve FCM quota (30%) for transactional order notification traffic",
             "Implement priority queue: transactional notifications preempt marketing batch",
         ],
         "rollback_plan": {"safety_check": "push notification p99 delivery < 5s for 10 consecutive minutes"},
         "completed_steps": ["incident_commander", "log_analysis", "rca_analysis", "summary"]},
    ]

    # Tag all baseline records as historical so analytics can distinguish them
    for record in BASELINE:
        record["historical_baseline"] = True
        record.setdefault("completed_steps", [])

    # Merge: live + persisted take priority; baseline fills in the historical window
    all_ids = live_ids | {str(item.get("incident_id")) for item in persisted}
    history = [item for item in BASELINE if str(item.get("incident_id")) not in all_ids]

    merged = [*live, *persisted, *history]
    return merged


def _default_agent_journey(service: str, cause: str = "RCA pending") -> List[Dict[str, Any]]:
    now = datetime.now().isoformat()
    steps = [
        ("Alert Triage Agent", "Normalized alert, severity, service and incident priority.", "Context Agent", "Alert scope requires service metadata."),
        ("Context Agent", f"Loaded ownership, environment, dependencies and runbooks for {service}.", "RCA Agent", "Context is complete enough for evidence correlation."),
        ("RCA Agent", f"Correlated logs, metrics and deployments. Leading hypothesis: {cause}.", "Remediation Agent", "RCA confidence crossed remediation-planning threshold."),
        ("Remediation Agent", "Prepared resolution plan with rollback and safety checks.", "Risk/Approval Agent", "Execution requires human approval guardrail."),
        ("Risk/Approval Agent", "Validated blast radius, rollback safety and audit policy.", "Escalation Agent", "High-impact changes require owner notification."),
        ("Learning Agent", "Captured summary, lessons and preventive actions for incident memory.", "Complete", "Investigation completed and memory updated."),
    ]
    return [
        {"agent": a, "action": a.lower().replace(" ", "_"), "reasoning": r, "next_agent": n, "handoff_reason": h, "timestamp": now, "span_id": f"span-{i+1}", "parent_span_id": f"span-{i}" if i else ""}
        for i, (a, r, n, h) in enumerate(steps)
    ]


def _agent_journey(record: Dict[str, Any]) -> Dict[str, Any]:
    spans = record.get("agent_invocations") or _default_agent_journey(str(record.get("service") or "unknown"), (record.get("root_cause") or {}).get("hypothesis", "RCA pending"))
    nodes = []
    edges = []
    seen = set()
    last_agent = None
    for index, span in enumerate(spans):
        agent = str(span.get("agent") or f"Agent {index+1}")
        if agent not in seen:
            nodes.append({"id": agent, "label": agent, "type": "agent", "status": "complete" if index < len(spans)-1 else record.get("agent_status", "current")})
            seen.add(agent)
        if last_agent and last_agent != agent:
            edges.append({"source": last_agent, "target": agent, "reason": str(span.get("handoff_reason") or span.get("reasoning") or "handoff")[:180]})
        if span.get("next_agent") and span.get("next_agent") != "Complete":
            nxt = str(span.get("next_agent"))
            if nxt not in seen:
                nodes.append({"id": nxt, "label": nxt, "type": "agent", "status": "pending"})
                seen.add(nxt)
            edges.append({"source": agent, "target": nxt, "reason": str(span.get("handoff_reason") or "planned handoff")[:180]})
        last_agent = agent
    return {"nodes": nodes, "edges": edges, "spans": spans}


def _resolution_answer_for_record(record: Dict[str, Any]) -> Dict[str, Any]:
    rc = record.get("root_cause") or {}
    recs = record.get("recovery_recommendations") or []
    rollback = record.get("rollback_plan") or {}
    owner = (record.get("ownership") or {}).get("primary", "service owner")
    safety = rollback.get("safety_check") or "error rate, latency and affected-user count return to normal"
    if not recs:
        recs = [rollback.get("strategy") or "execute the service runbook remediation", "validate KPIs and keep rollback ready"]
    return {
        "summary": f"Resolve {record.get('service')} by addressing: {rc.get('hypothesis', 'the leading RCA')}",
        "steps": recs[:5],
        "approval_required": True,
        "owner": owner,
        "rollback": rollback,
        "safety_check": safety,
        "confidence": rc.get("confidence", record.get("rca_confidence", 0.72)),
    }


def _looks_like_resolution_question(question: str) -> bool:
    q = question.lower()
    return any(
        term in q
        for term in (
            "resolution",
            "resolve",
            "remediate",
            "remediation",
            "fix",
            "solution",
            "recover",
            "recovery",
            "what should we do",
            "next step",
        )
    )


def _resolution_text(record: Dict[str, Any]) -> str:
    resolution = _resolution_answer_for_record(record)
    steps = resolution.get("steps") or []
    summary = str(resolution.get("summary") or "").strip()
    owner = str(resolution.get("owner") or "service owner")
    safety = str(resolution.get("safety_check") or "validate KPIs before closure")
    confidence = float(resolution.get("confidence") or 0.0)
    step_text = " ".join(f"({index + 1}) {step}" for index, step in enumerate(steps[:4]))
    return (
        f"{summary}. "
        f"Recommended resolution: {step_text} "
        f"Owner: {owner}. Safety check: {safety}. "
        f"Confidence: {confidence * 100:.0f}%."
    ).strip()


def _resolution_citation(record: Dict[str, Any]) -> Dict[str, Any]:
    incident_id = str(record.get("incident_id") or "")
    root_cause = record.get("root_cause") or {}
    return {
        "title": f"Incident resolution: {record.get('service', 'unknown')}",
        "source_path": f"incident://{incident_id}",
        "kind": "incident",
        "content": f"{record.get('alert_description') or ''}; root_cause={root_cause.get('hypothesis') or 'pending'}",
        "score": 0.97,
        "citation": f"incident://{incident_id}",
    }


def _answer_incident_resolution_lookup(question: str) -> Dict[str, Any] | None:
    if not _looks_like_resolution_question(question):
        return None
    matches = _matching_incident_records(question)
    if not matches:
        return None

    # Enrich baseline records that don't have service profile yet
    for r in matches:
        if not r.get("service_profile"):
            enrich_record(r)

    # Take up to 4 top-ranked incidents for a comprehensive response
    top = matches[:4]
    severity_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
    blocks: List[str] = []
    citations: List[Dict[str, Any]] = []
    total_users = 0
    total_impact = 0.0

    for record in top:
        rc = record.get("root_cause") or {}
        svc = record.get("service", "unknown")
        desc = record.get("alert_description", "")
        hypothesis = rc.get("hypothesis", "RCA pending")
        confidence = float(rc.get("confidence") or record.get("rca_confidence") or 0.72)
        affected = int(record.get("affected_users") or 0)
        impact = float(record.get("estimated_revenue_impact_per_minute") or 0.0)
        severity = str(record.get("severity", "medium")).lower()
        icon = severity_icon.get(severity, "⚪")
        iid = str(record.get("incident_id") or "")
        created = str(record.get("created_at") or "")[:10]

        total_users += affected
        total_impact += impact

        resolution_data = _resolution_answer_for_record(record)
        steps = resolution_data.get("steps") or []
        owner = str(resolution_data.get("owner") or (record.get("ownership") or {}).get("primary") or "platform-oncall")
        safety = str(resolution_data.get("safety_check") or "validate error rate < 0.1% before closure")

        step_text = " → ".join(f"({i+1}) {s}" for i, s in enumerate(steps[:4]))
        if not step_text:
            step_text = "(1) Isolate and roll back the offending change (2) Validate KPIs stabilize"

        blocks.append(
            f"{icon} [{severity.upper()}] {svc.upper()} — {desc} ({created})\n"
            f"   Root Cause: {hypothesis} (confidence: {confidence*100:.0f}%)\n"
            f"   Resolution: {step_text}\n"
            f"   Owner: {owner} | Safety check: {safety}\n"
            f"   Impact: {affected:,} users affected, ${impact:,.0f}/min revenue loss"
        )
        citations.append({
            "title": f"Incident resolution — {svc} [{iid[:8]}]",
            "source_path": f"incident://{iid}",
            "kind": "incident",
            "content": f"{desc}; root_cause={hypothesis}; affected={affected} users; impact=${impact}/min",
            "score": round(confidence, 3),
            "citation": f"incident://{iid}",
        })

    header = (
        f"Found {len(top)} matching resolved incident(s). "
        f"Combined impact: {total_users:,} users, ${total_impact:,.0f}/min revenue exposure.\n\n"
    )
    answer = header + "\n\n".join(blocks)

    return {
        "answer": answer,
        "confidence": round(float((top[0].get("root_cause") or {}).get("confidence") or 0.72), 3),
        "citations": citations,
        "follow_ups": [
            "Do you want the full agent investigation journey for any of these incidents?",
            "Should I show all supporting evidence and log anomalies?",
            "Which service should I prioritize for preventive action?",
        ],
        "language": "en",
        "knowledge": {"query": question, "results": citations, "confidence": 0.97},
    }


def _parse_uploaded_log(content: str, filename: str) -> Dict[str, Any]:
    lower = content.lower()
    fname = filename.lower().replace("-", " ").replace("_", " ")

    # ── Service detection: filename wins, then content scan ──────────────────
    INFRA_PATTERNS = {
        "k8s-control-plane":  ["k8s", "kubernetes", "control plane", "kube", "etcd", "apiserver", "kubelet", "scheduler", "controller"],
        "etcd":               ["etcd", "quorum", "raft", "member", "cluster election"],
        "kafka":              ["kafka", "broker", "consumer", "producer", "partition", "offset", "zookeeper"],
        "redis-cache":        ["redis", "sentinel", "replication", "aof", "rdb"],
        "nginx-ingress":      ["nginx", "ingress", "upstream", "vhost"],
        "postgres-primary":   ["postgres", "postgresql", "pg_", "replication slot", "wal"],
        "payment-api":        ["payment", "db pool", "connection pool", "jdbc"],
        "order-processor":    ["order", "fulfillment", "outofmemory", "gc pause", "heap"],
        "checkout-gateway":   ["checkout", "cart", "apple pay", "stripe"],
        "search-api":         ["search", "elasticsearch", "solr", "cache stampede"],
        "fraud-service":      ["fraud", "feature store", "ml model", "scoring"],
        "notification-service": ["notification", "fcm", "push", "apns", "sms"],
        "catalog-api":        ["catalog", "product", "sku", "cdn"],
    }

    service = "unknown"
    # Filename-first detection (highest signal)
    for svc, keywords in INFRA_PATTERNS.items():
        if any(kw in fname for kw in keywords):
            service = svc
            break
    # Content scan fallback
    if service == "unknown":
        for svc, keywords in INFRA_PATTERNS.items():
            if any(kw in lower for kw in keywords):
                service = svc
                break
    # Catalog match override if richer
    catalog_matches = [c for c in SERVICE_CATALOG if re.search(rf"(?:^|\W){re.escape(c)}(?:$|\W)", lower)]
    if catalog_matches and service == "unknown":
        service = min(catalog_matches, key=lambda c: lower.index(c))

    # ── Severity ─────────────────────────────────────────────────────────────
    severity = (
        "critical" if any(x in lower for x in ["critical", "sev0", "sev1", "fatal", "panic", "quorum", "data loss", "split brain"])
        else "high" if any(x in lower for x in ["error", "exception", "timeout", "failed", "unavailable"])
        else "medium"
    )

    # ── RCA Hypothesis: pattern-matched, specific ────────────────────────────
    hypothesis = "Elevated error rate detected; requires deeper evidence correlation."
    rca_confidence = 0.60
    resolution_steps: List[str] = []
    engineering_notes = ""
    executive_summary_text = ""

    if any(x in lower for x in ["etcd", "quorum", "raft", "leader election", "member"]):
        hypothesis = "etcd quorum loss — raft leader election failure causing Kubernetes control plane unavailability."
        rca_confidence = 0.88
        resolution_steps = [
            "Identify failed etcd member(s): etcdctl endpoint status --cluster",
            "Check etcd disk I/O saturation (fio benchmark) — etcd is I/O sensitive",
            "If member is down, restore from snapshot: etcdctl snapshot restore",
            "Re-add member to cluster: etcdctl member add <name> --peer-urls=<url>",
            "Verify quorum restored: etcdctl endpoint health --cluster",
            "Restart kube-apiserver after quorum is confirmed",
        ]
        engineering_notes = (
            f"etcd quorum failure on {service}. Raft consensus requires ⌊N/2⌋+1 members healthy. "
            "Root cause: network partition or disk I/O starvation causing leader election timeout. "
            "Control plane (kube-apiserver, scheduler, controller-manager) becomes read-only or unavailable "
            "during quorum loss — no new pods scheduled, no deployments possible."
        )
        executive_summary_text = (
            f"The Kubernetes control plane became unavailable due to an etcd quorum failure. "
            "No new workloads could be scheduled and existing automated recovery mechanisms were disabled. "
            "Engineering restored the etcd cluster quorum and control plane functionality. "
            "No data loss occurred; all running workloads continued unaffected during the control plane outage."
        )
    elif any(x in lower for x in ["connection pool", "db pool", "jdbc", "hikaricp"]):
        hypothesis = "Database connection pool exhaustion — service cannot acquire DB connections."
        rca_confidence = 0.91
        resolution_steps = [
            "Revert connection pool config to pre-incident values (max_pool_size)",
            "Enable pool metrics alerting: db.pool.waitTime > 500ms",
            "Run connection pool validator script to confirm saturation cleared",
            "Apply idle-timeout tuning to prevent future exhaustion",
        ]
        engineering_notes = "DB connection pool exhausted. Likely cause: pool size misconfigured after recent deploy or connection leak."
        executive_summary_text = f"A database connection pool outage impacted {service}, preventing payment processing. Configuration was corrected and the service restored."
    elif any(x in lower for x in ["kafka", "broker", "partition", "offset lag"]):
        hypothesis = "Kafka broker failure or consumer lag causing message processing backlog."
        rca_confidence = 0.83
        resolution_steps = [
            "Check broker leadership: kafka-topics.sh --describe",
            "Restart failed broker and verify ISR recovery",
            "Reset consumer group offsets if lag is unrecoverable",
            "Scale consumers to catch up on backlog",
        ]
        engineering_notes = "Kafka partition leadership issue or consumer group rebalance storm detected in logs."
        executive_summary_text = f"Event streaming disruption on {service} caused message processing delays. Kafka cluster stability was restored."
    elif any(x in lower for x in ["outofmemory", "gc pause", "heap", "oom"]):
        hypothesis = "JVM heap exhaustion or GC pressure causing worker instability."
        rca_confidence = 0.85
        resolution_steps = [
            "Increase heap allocation: -Xmx4g or tune GC settings",
            "Enable G1GC or ZGC for lower pause times",
            "Profile heap with async-profiler or JFR for memory leak",
            "Add GC pause alerting: gc_pause_p95 > 500ms",
        ]
        engineering_notes = "OutOfMemoryError or sustained GC pause detected. Worker threads stalled during GC stop-the-world events."
        executive_summary_text = f"Memory pressure on {service} caused service degradation. Heap sizing was corrected and workers restarted."
    elif any(x in lower for x in ["timeout", "circuit breaker", "retry storm"]):
        hypothesis = "Downstream timeout cascade or retry amplification causing service overload."
        rca_confidence = 0.79
        resolution_steps = [
            "Enable circuit breaker on downstream dependency",
            "Reduce retry max attempts and add exponential backoff",
            "Add timeout budget headers to downstream calls",
            "Check dependent service health dashboards",
        ]
        engineering_notes = "Cascading timeouts detected — likely a downstream dependency degraded, triggering retry amplification."
        executive_summary_text = f"A cascading timeout failure impacted {service} due to a degraded downstream dependency. Circuit breakers were enabled to restore stability."
    elif any(x in lower for x in ["redis", "sentinel", "replication"]):
        hypothesis = "Redis sentinel failover failure or replication lag causing cache unavailability."
        rca_confidence = 0.80
        resolution_steps = [
            "Check sentinel quorum: redis-cli -p 26379 sentinel masters",
            "Force failover if primary unresponsive: sentinel failover <master-name>",
            "Verify replica replication offset convergence",
            "Update application Redis connection to new primary address",
        ]
        engineering_notes = "Redis sentinel did not complete automatic failover. Cache reads/writes failing, causing application fallback paths to activate."
        executive_summary_text = f"Cache layer ({service}) experienced a failover failure. Manual intervention restored Redis sentinel quorum."
    elif any(x in lower for x in ["nginx", "ingress", "upstream", "502", "503"]):
        hypothesis = "NGINX ingress upstream failure — backend pods unhealthy or misconfigured."
        rca_confidence = 0.82
        resolution_steps = [
            "Check upstream pod health: kubectl get pods -n <namespace>",
            "Review NGINX upstream config for correct service endpoints",
            "Scale up backend deployment if pods are OOM-killed",
            "Check ingress controller logs for upstream_addr errors",
        ]
        engineering_notes = "502/503 errors from NGINX ingress indicating all upstream backends failing health checks."
        executive_summary_text = f"Ingress routing failures ({service}) caused customer-facing errors. Upstream pods were restored to health."
    else:
        hypothesis = "Service degradation detected from uploaded logs. Full RCA pending deeper evidence correlation."
        engineering_notes = f"Log scan found elevated error signals in {filename}. Manual triage recommended."
        executive_summary_text = f"Elevated error signals were detected in {service} logs. Engineering is investigating the root cause."

    error_lines = [
        line.strip() for line in content.splitlines()
        if any(k in line.lower() for k in ["error", "exception", "timeout", "failed", "critical", "fatal", "panic", "quorum"])
    ]

    # Estimate impact from log signals
    affected_users = 0
    revenue_per_min = 0.0
    if severity == "critical":
        affected_users = 15000
        revenue_per_min = 750.0
    elif severity == "high":
        affected_users = 5000
        revenue_per_min = 200.0

    record = {
        "incident_id": f"upload-{uuid.uuid4().hex[:8]}",
        "trace_id": filename,
        "timestamp": datetime.now().isoformat(),
        "created_at": datetime.now().isoformat(),
        "service": service,
        "severity": severity,
        "alert_description": f"Uploaded log analysis: {filename}",
        "current_status": "complete",
        "lifecycle_status": "needs_human_review",
        "root_cause": {
            "hypothesis": hypothesis,
            "confidence": rca_confidence,
            "supporting_evidence": error_lines[:5],
        },
        "log_anomalies": [{"type": "uploaded_error_lines", "count": len(error_lines), "examples": error_lines[:3]}],
        "recovery_recommendations": resolution_steps or [
            "Execute the relevant service runbook remediation.",
            "Validate safety check: error rate and latency return to baseline.",
        ],
        "executive_summary": executive_summary_text,
        "engineering_summary": engineering_notes,
        "affected_users": affected_users,
        "estimated_revenue_impact_per_minute": revenue_per_min,
        "rca_confidence": rca_confidence,
        "agent_invocations": _default_agent_journey(service, hypothesis),
        "uploaded": True,
        "completed_steps": ["incident_commander", "log_analysis", "metrics_analysis", "rca_analysis", "business_impact", "summary"],
    }
    enrich_record(record)
    record["context_metadata"] = build_incident_context(record)
    # Ensure recovery_recommendations populated from context if still empty
    if not record.get("recovery_recommendations"):
        record["recovery_recommendations"] = [
            record.get("rollback_plan", {}).get("strategy") or "Execute the relevant service runbook remediation.",
            f"Escalate to {(record.get('ownership') or {}).get('primary', 'service owner')} with uploaded evidence.",
        ]
    return record



@app.on_event("startup")
async def _startup_knowledge_base() -> None:
    initialize_knowledge_base(Path("."))
    # Pre-warm the Jarvis context cache so first chat request is fast
    import asyncio
    asyncio.get_event_loop().call_later(2.0, lambda: _jarvis_system_context(force_refresh=True))


def _save_reinvestigation_jobs() -> None:
    try:
        os.makedirs(os.path.dirname(REINVESTIGATION_JOBS_PATH), exist_ok=True)
        with open(REINVESTIGATION_JOBS_PATH, "w", encoding="utf-8") as f:
            json.dump(reinvestigation_jobs, f, indent=2)
    except Exception as exc:
        print(f"[app] failed to persist reinvestigation jobs: {exc}")


async def _post_slack_update(
    record: Dict[str, Any],
    text: str,
    *,
    include_blocks: bool = False,
) -> None:
    channel = str(record.get("slack_channel_id") or "").strip()
    if not channel:
        return
    try:
        response = await slack_assistant.post_message(
            channel=channel,
            text=text,
            blocks=incident_blocks(record, slack_assistant) if include_blocks else None,
            thread_ts=record.get("slack_thread_ts"),
        )
        if response.get("ok") and response.get("ts") and not record.get("slack_thread_ts"):
            record["slack_thread_ts"] = response["ts"]
    except Exception as exc:
        print(f"[slack] update failed: {exc}")


def _normalize_assistant_response(payload: Dict[str, Any], source: str) -> Dict[str, Any]:
    response = dict(payload)
    response.setdefault("source", source)
    response.setdefault("citations", [])
    response.setdefault("follow_ups", [])
    response.setdefault("confidence", 0.0)
    response.setdefault("language", "en")
    response.setdefault(
        "routing",
        {
            "cache_hit": False,
            "tier": "graph_memory",
            "provider": "platform-memory",
            "model": "deterministic",
            "fallback_reason": None,
        },
    )
    return response


_INCIDENT_LOOKUP_TERMS = {
    "incident", "incidents", "incidence", "occurred", "occurrence",
    "show", "list", "find", "respective",
}
_INCIDENT_STOP_WORDS = {
    "a", "an", "and", "are", "can", "could", "due", "for", "from",
    "i", "in", "is", "me", "of", "please", "the", "to", "we", "you",
}
_INCIDENT_TERM_ALIASES = {"db": "database", "incidence": "incident"}


def _incident_terms(text: str) -> set[str]:
    terms = re.findall(r"[a-z0-9]+", text.lower())
    return {
        _INCIDENT_TERM_ALIASES.get(term, term)
        for term in terms
        if term not in _INCIDENT_STOP_WORDS
    }


def _answer_incident_lookup(question: str) -> Dict[str, Any] | None:
    """Answer global discovery questions from live incidents before repo docs."""
    query_terms = _incident_terms(question)
    if not query_terms or not (query_terms & _INCIDENT_LOOKUP_TERMS):
        return None

    matches = _matching_incident_records(question)
    if not matches:
        return None

    lines: List[str] = []
    citations: List[Dict[str, Any]] = []
    for record in matches[:5]:
        incident_id = str(record.get("incident_id", ""))
        root_cause = record.get("root_cause") or {"hypothesis": record.get("hypothesis")}
        status = record.get("current_status") or ("resolved" if record.get("_from_memory") else "unknown")
        description = record.get("alert_description") or root_cause.get("hypothesis") or "No alert description"
        rca = root_cause.get("hypothesis")
        rca_text = f" Root cause: {rca}." if rca else ""
        lines.append(
            f"{record.get('service', 'unknown')} incident {incident_id[:8]}: "
            f"{description}. Status: {status}.{rca_text} Open: /incident/{incident_id}"
        )
        citations.append(
            {
                "title": f"Incident: {record.get('service', 'unknown')}",
                "source_path": f"incident://{incident_id}",
                "kind": "incident",
                "content": f"{description}; status={status}; root_cause={rca or 'pending'}",
                "score": 0.95,
                "citation": f"incident://{incident_id}",
            }
        )

    noun = "incident" if len(lines) == 1 else "incidents"
    return {
        "answer": f"I found {len(lines)} matching {noun}:\n" + "\n".join(lines),
        "confidence": 0.95,
        "citations": citations,
        "follow_ups": [
            "Would you like the evidence and logs for this incident?",
            "Should I summarize its impact and recommended recovery actions?",
        ],
        "language": "en",
        "knowledge": {"query": question, "results": citations, "confidence": 0.95},
    }


def _matching_incident_records(question: str) -> List[Dict[str, Any]]:
    """Rank live, persisted, and baseline incidents by query-term overlap."""
    query_terms = _incident_terms(question) - _INCIDENT_LOOKUP_TERMS
    if not query_terms:
        return []

    records = _analytics_records_with_history()
    ranked: List[tuple[int, Dict[str, Any]]] = []
    for record in records:
        if not record:
            continue
        root_cause = record.get("root_cause") or {"hypothesis": record.get("hypothesis")}
        searchable = " ".join(
            str(value or "")
            for value in (
                record.get("service"),
                record.get("alert_description"),
                root_cause.get("hypothesis"),
                record.get("engineering_summary"),
            )
        )
        overlap = query_terms & _incident_terms(searchable)
        if overlap:
            ranked.append((len(overlap), record))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [record for _, record in ranked]


def _qa_incident_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Convert the compact persisted-memory shape into the Q&A record shape."""
    if not record.get("_from_memory"):
        return record
    return {
        **record,
        "current_status": "resolved",
        "root_cause": {
            "hypothesis": record.get("hypothesis"),
            "confidence": record.get("confidence", 0.0),
        },
    }


_jarvis_ctx_cache: Dict[str, Any] = {}
_jarvis_ctx_ts: float = 0.0
_JARVIS_CTX_TTL = 45.0  # seconds


def _jarvis_system_context(force_refresh: bool = False) -> Dict[str, Any]:
    """Build a bounded, secret-free snapshot for the system-wide Jarvis page.
    Cached for 45 seconds to avoid blocking every chat request."""
    import time
    global _jarvis_ctx_cache, _jarvis_ctx_ts
    now = time.monotonic()
    if not force_refresh and _jarvis_ctx_cache and (now - _jarvis_ctx_ts) < _JARVIS_CTX_TTL:
        return _jarvis_ctx_cache

    records = _analytics_records_with_history()[:25]

    def incident_summary(item: Dict[str, Any]) -> Dict[str, Any]:
        root_cause = item.get("root_cause") or {}
        return {
            "incident_id": item.get("incident_id"),
            "service": item.get("service"),
            "severity": item.get("severity"),
            "alert_description": str(item.get("alert_description") or "")[:300],
            "current_status": item.get("current_status"),
            "root_cause": root_cause.get("hypothesis"),
            "root_cause_confidence": root_cause.get("confidence"),
            "affected_users": item.get("affected_users"),
            "impact_per_minute": item.get("estimated_revenue_impact_per_minute"),
            "recommendations": (item.get("recovery_recommendations") or [])[:3],
        }

    connector_summaries = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "type": item.get("type"),
            "status": item.get("status"),
            "last_heartbeat": item.get("last_heartbeat"),
        }
        for item in list_connectors()[:30]
    ]
    ctx = {
        "incidents": [incident_summary(item) for item in records[:12]],
        "analytics": incident_analytics(records, "week"),
        "analytics_by_period": {
            "day": incident_analytics(records, "day"),
            "week": incident_analytics(records, "week"),
            "month": incident_analytics(records, "month"),
        },
        "connectors": connector_summaries,
        "platform": {
            "agentic": True,
            "llm_provider": get_provider() or "none",
            "llm_model": get_model(),
            "war_room_configured": war_room_configured(),
            "teams_configured": teams_configured(),
        },
    }
    _jarvis_ctx_cache = ctx
    _jarvis_ctx_ts = now
    return ctx


def _impact_threshold() -> float:
    configured = teams_runtime_config().get("alert_threshold_per_minute") or os.getenv("HIGH_IMPACT_ALERT_THRESHOLD_PER_MINUTE", "1000")
    try:
        return max(0.0, float(configured))
    except (TypeError, ValueError):
        return 1000.0


def _is_high_impact_record(record: Dict[str, Any]) -> tuple[bool, str]:
    if record.get("high_impact") or record.get("alert_tier") == "high_impact":
        return True, "marked high impact at intake"
    severity = str(record.get("severity") or "").casefold()
    impact = float(record.get("estimated_revenue_impact_per_minute") or 0.0)
    threshold = _impact_threshold()
    if severity in {"critical", "sev0", "sev1"} and impact >= threshold:
        return True, f"critical incident impact ${impact:.2f}/min exceeds ${threshold:.2f}/min"
    return False, ""


async def _maybe_raise_high_impact_teams_alert(record: Dict[str, Any]) -> None:
    if record.get("teams_alert_sent"):
        return
    if not teams_configured():
        return
    should_alert, reason = _is_high_impact_record(record)
    if not should_alert:
        return
    await post_teams_alert(record, reason)
    record["teams_alert_sent"] = True
    record["teams_alert_reason"] = reason
    record["teams_alerted_at"] = datetime.now().isoformat()


async def _create_incident(
    incident_data: Dict[str, Any],
    *,
    slack_context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    incident_id: str = str(uuid.uuid4())
    timestamp: str = incident_data.get("timestamp", datetime.now().isoformat())

    state: IncidentState = IncidentState(
        incident_id=incident_id,
        timestamp=timestamp,
        alert_description=incident_data.get("alert_description", ""),
        service=incident_data.get("service", "unknown"),
        severity=incident_data.get("severity", "unknown"),
    )
    state.trace_id = incident_id

    record: Dict[str, Any] = _serialize_state(dict(vars(state)))
    enrich_record(record)
    record["context_metadata"] = build_incident_context(record)
    record["current_status"] = "investigating"
    record["agent_status"] = "investigating"
    record["lifecycle_status"] = "investigating"
    record["created_at"] = datetime.now().isoformat()
    record["high_impact"] = bool(incident_data.get("high_impact") or incident_data.get("alert_tier") == "high_impact")
    if incident_data.get("alert_tier"):
        record["alert_tier"] = incident_data.get("alert_tier")
    if slack_context:
        record.update(
            {
                "slack_channel_id": slack_context.get("channel_id"),
                "slack_thread_ts": slack_context.get("thread_ts"),
                "slack_user_id": slack_context.get("user_id"),
            }
        )
    incident_store[incident_id] = record
    incident_order.insert(0, incident_id)
    upsert_incident_graph(record)
    await _maybe_raise_high_impact_teams_alert(record)

    await post_war_room(
        f"ðŸš¨ Incident opened on *{state.service}* ({state.severity.upper()}): "
        f"{state.alert_description} â€” agents dispatched. "
        f"Live: {WEB_BASE_URL}/incident/{incident_id}"
    )
    await _post_slack_update(
        record,
        f"ðŸš¨ Incident opened: {state.service} â€” agents dispatched.",
        include_blocks=True,
    )
    asyncio.create_task(_run_analysis(incident_id, state))
    return record


def _serialize_state(values: Dict[str, Any]) -> Dict[str, Any]:
    completed: Any = values.get("completed_steps", set())
    quality_gates: Dict[str, Any] = values.get("quality_gates") or evaluate_quality_gates(values)
    return {
        "incident_id": values.get("incident_id"),
        "trace_id": values.get("trace_id") or values.get("incident_id"),
        "timestamp": values.get("timestamp"),
        "alert_description": values.get("alert_description"),
        "service": values.get("service"),
        "severity": values.get("severity"),
        "lifecycle_status": values.get("lifecycle_status", "opened"),
        "agent_status": values.get("agent_status") or values.get("current_status", "initial"),
        "analysis_iterations": values.get("analysis_iterations", 0),
        "rca_confidence": values.get("rca_confidence", 0.0),
        "current_status": values.get("current_status", "initial"),
        "completed_steps": sorted(completed) if completed else [],
        "evidence_catalog": values.get("evidence_catalog", {}),
        "log_anomalies": values.get("log_anomalies", []),
        "log_context_cache": values.get("log_context_cache", {}),
        "metric_anomalies": values.get("metric_anomalies", []),
        "deployment_changes": values.get("deployment_changes", []),
        "root_cause": values.get("root_cause"),
        "affected_users": values.get("affected_users", 0),
        "estimated_revenue_impact_per_minute": values.get(
            "estimated_revenue_impact_per_minute", 0.0
        ),
        "estimated_cost_impact_per_minute": values.get(
            "estimated_cost_impact_per_minute", 0.0
        ),
        "revenue_impact_justification": values.get(
            "revenue_impact_justification", {}
        ),
        "business_risk_level": values.get("business_risk_level", "unknown"),
        "engineering_summary": values.get("engineering_summary", ""),
        "executive_summary": values.get("executive_summary", ""),
        "recovery_recommendations": values.get("recovery_recommendations", []),
        "troubleshooting_plan": values.get("troubleshooting_plan", []),
        "stakeholder_updates": values.get("stakeholder_updates", {}),
        "kpi_guardrails": values.get("kpi_guardrails", {}),
        "escalation_summary": values.get("escalation_summary", ""),
        "similar_incidents": values.get("similar_incidents", []),
        "agent_invocations": values.get("agent_invocations", []),
        "compact_contexts": values.get("compact_contexts", []),
        "review_events": values.get("review_events", []),
        "service_profile": values.get("service_profile", {}),
        "ownership": values.get("ownership", {}),
        "environment": values.get("environment", {}),
        "dependencies": values.get("dependencies", []),
        "upstream_services": values.get("upstream_services", []),
        "runbooks": values.get("runbooks", []),
        "escalation_path": values.get("escalation_path", []),
        "rollback_plan": values.get("rollback_plan", {}),
        "blast_radius": values.get("blast_radius", {}),
        "context_metadata": values.get("context_metadata", {}),
        "source_connector_id": values.get("source_connector_id"),
        "debate_rounds": values.get("debate_rounds", []),
        "quality_gates": quality_gates,
    }


def _state_from_record(record: Dict[str, Any]) -> IncidentState:
    state = IncidentState(
        incident_id=str(record.get("incident_id")),
        timestamp=str(record.get("timestamp")),
        alert_description=str(record.get("alert_description", "")),
        service=str(record.get("service", "unknown")),
        severity=str(record.get("severity", "unknown")),
    )
    state.trace_id = str(record.get("trace_id") or record.get("incident_id"))
    state.review_events = list(record.get("review_events", []))
    state.agent_invocations = list(record.get("agent_invocations", []))
    state.debate_rounds = list(record.get("debate_rounds", []))
    state.span_seq = len(state.agent_invocations)
    if state.agent_invocations:
        state.current_parent_span_id = str(
            state.agent_invocations[-1].get("span_id", "")
        )
    state.max_iterations = int(record.get("max_iterations", 5) or 5)
    state.analysis_iterations = int(record.get("analysis_iterations", 0) or 0)
    state.lifecycle_status = "investigating"
    state.current_status = "reinvestigating"
    state.agent_status = "reinvestigating"
    state.context_metadata = record.get("context_metadata", {})
    return state


async def _run_analysis(incident_id: str, state: IncidentState) -> None:
    """Run the agent graph with a per-node callback so the dashboard gets
    live updates after every agent node (ainvoke + callback, since this
    LangGraph version does not expose astream on compiled graphs)."""
    graph: Any = get_compiled_graph()
    notified: set = set()

    def _on_node_update(state_dict: Dict[str, Any]) -> None:
        """Called by _as_updates() inside every node after it completes."""
        if incident_id not in incident_store:
            return
        previous_record = incident_store[incident_id]
        record: Dict[str, Any] = _serialize_state(state_dict)
        enrich_record(record)
        record["created_at"] = previous_record.get("created_at")
        if previous_record.get("remediation_decisions"):
            record["remediation_decisions"] = previous_record["remediation_decisions"]
        for preserved in ("high_impact", "alert_tier", "teams_alert_sent", "teams_alert_reason", "teams_alerted_at"):
            if previous_record.get(preserved) is not None:
                record[preserved] = previous_record[preserved]
        if previous_record.get("review_events") and not record.get("review_events"):
            record["review_events"] = previous_record.get("review_events", [])
        record["context_metadata"] = build_incident_context(record)
        incident_store[incident_id] = record
        upsert_incident_graph(record)

        root_cause: Dict[str, Any] = record.get("root_cause") or {}
        if root_cause and "rca" not in notified:
            notified.add("rca")
            deploy_note = (
                f" ⚡ {root_cause['deploy_correlation']}"
                if root_cause.get("deploy_correlation") else ""
            )
            asyncio.create_task(post_war_room(
                f"🔍 Root cause identified for *{record.get('service')}*: "
                f"{root_cause.get('hypothesis')} "
                f"({root_cause.get('confidence', 0) * 100:.0f}% confidence)."
                f"{deploy_note}"
            ))
            asyncio.create_task(_post_slack_update(
                record,
                f"Root cause identified: {root_cause.get('hypothesis')} "
                f"({root_cause.get('confidence', 0) * 100:.0f}% confidence).",
                include_blocks=True,
            ))

    try:
        set_node_callback(_on_node_update)
        await graph.ainvoke(
            dict(vars(state)),
            config={"recursion_limit": 80},
        )
    except Exception as exc:
        print(f"[app] analysis failed for {incident_id}: {exc}")
        import traceback; traceback.print_exc()
        incident_store[incident_id]["current_status"] = "failed"
        incident_store[incident_id]["agent_status"] = "failed"
        incident_store[incident_id]["lifecycle_status"] = "failed"
        incident_store[incident_id]["error"] = str(exc)
        upsert_incident_graph(incident_store[incident_id])
        if incident_id in reinvestigation_jobs:
            reinvestigation_jobs[incident_id]["status"] = "failed"
            reinvestigation_jobs[incident_id]["error"] = str(exc)
            _save_reinvestigation_jobs()
        return
    finally:
        clear_node_callback()

    # Post-completion book-keeping
    if incident_store.get(incident_id, {}).get("current_status") != "failed":
        if incident_store[incident_id].get("current_status") != "complete":
            incident_store[incident_id]["current_status"] = "complete"
        incident_store[incident_id]["agent_status"] = "complete"
        incident_store[incident_id]["quality_gates"] = evaluate_quality_gates(
            incident_store[incident_id]
        )
        if incident_store[incident_id]["quality_gates"].get("overall_passed"):
            incident_store[incident_id]["lifecycle_status"] = "needs_human_review"
        upsert_incident_graph(incident_store[incident_id])
        await _maybe_raise_high_impact_teams_alert(incident_store[incident_id])
        incident_memory_agent(incident_store[incident_id])

        final: Dict[str, Any] = incident_store[incident_id]
        if incident_id in reinvestigation_jobs:
            reinvestigation_jobs[incident_id]["status"] = "complete"
            reinvestigation_jobs[incident_id]["completed_at"] = datetime.now().isoformat()
            _save_reinvestigation_jobs()
        await post_war_room(
            f"✅ Investigation complete for *{final.get('service')}*: "
            f"{(final.get('root_cause') or {}).get('hypothesis', 'unknown cause')}. "
            f"{final.get('affected_users', 0):,} users affected, "
            f"${final.get('estimated_revenue_impact_per_minute', 0):.2f}/min revenue impact. "
            f"Full report: {WEB_BASE_URL}/incident/{incident_id}"
        )
        await _post_slack_update(
            final,
            f"Investigation complete.\n{summarize_incident(final)}",
            include_blocks=True,
        )


def _start_reinvestigation(incident_id: str, actor: str, reason: str) -> None:
    record = incident_store[incident_id]
    state = _state_from_record(record)
    reinvestigation_jobs[incident_id] = {
        "status": "running",
        "requested_at": datetime.now().isoformat(),
        "actor": actor,
        "reason": reason,
    }
    _save_reinvestigation_jobs()
    record["current_status"] = "reinvestigating"
    record["agent_status"] = "reinvestigating"
    record["lifecycle_status"] = "investigating"
    asyncio.create_task(_run_analysis(incident_id, state))


def _resolve_incident_id(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        raise HTTPException(status_code=404, detail="Incident not found")
    if token in incident_store:
        return token
    matches = [incident_id for incident_id in incident_order if incident_id.startswith(token)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(status_code=400, detail="Incident id prefix is ambiguous")
    raise HTTPException(status_code=404, detail="Incident not found")


def _incident_for_slack_thread(channel_id: str, thread_ts: str | None) -> Dict[str, Any] | None:
    if not channel_id or not thread_ts:
        return None
    for incident_id in incident_order:
        record = incident_store.get(incident_id, {})
        if (
            record.get("slack_channel_id") == channel_id
            and record.get("slack_thread_ts") == thread_ts
        ):
            return record
    return None


async def _handle_slack_message_event(event: Dict[str, Any]) -> None:
    channel_id = str(event.get("channel", ""))
    thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
    text = str(event.get("text", "")).strip()
    user_id = str(event.get("user", "slack-user"))
    if not channel_id or not text:
        return
    cleaned = " ".join(part for part in text.split() if not part.startswith("<@"))
    command, rest = parse_command(cleaned)

    if command in {"trigger", "open", "start"}:
        await _create_incident(
            scenario_from_text(rest),
            slack_context={
                "channel_id": channel_id,
                "thread_ts": thread_ts if event.get("thread_ts") else None,
                "user_id": user_id,
            },
        )
        return

    record = _incident_for_slack_thread(channel_id, thread_ts)
    if not record:
        if command in {"help", "h"}:
            await slack_assistant.post_message(
                channel=channel_id,
                thread_ts=thread_ts,
                text=slack_assistant.home_text(),
            )
        return

    payload, source = await answer_question(record, cleaned)
    await slack_assistant.post_message(
        channel=channel_id,
        thread_ts=record.get("slack_thread_ts") or thread_ts,
        text=f"{payload.get('answer', '')}\n_Source: {source}_",
    )


@app.get("/api/health")
async def health_check() -> Dict[str, str]:
    return {"status": "healthy"}


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    provider: Any = get_provider()
    return {
        "llm_provider": provider or "heuristic",
        "llm_model": get_model(),
        "agentic": True,
        "war_room": war_room_configured(),
        "slack_assistant": slack_assistant.enabled,
    }


# `/api/graph` removed — agent-state Mermaid visualization endpoint deprecated.


@app.get("/api/slack/config")
async def get_slack_config() -> Dict[str, Any]:
    return {
        "enabled": slack_assistant.enabled,
        "outbound_configured": slack_assistant.require_configured_for_outbound(),
        "default_channel_configured": bool(slack_assistant.default_channel),
        "commands_endpoint": "/api/slack/commands",
        "events_endpoint": "/api/slack/events",
        "interactivity_endpoint": "/api/slack/interactivity",
    }


@app.get("/api/jarvis/status")
async def get_jarvis_status(_: None = Depends(require_api_access)) -> Dict[str, Any]:
    """Expose secret-free routing and local-runtime health for the Jarvis page."""
    context = _jarvis_system_context()
    routing = public_routing_config()
    provider = get_provider() or "none"
    model = get_model() or "unknown"
    # Keep the response focused: routing, selected provider/model, and cache stats.
    return {
        "routing": routing,
        "model": model,
        "provider": provider,
        "llm_ready": llm_available(),
        "cache": cache_stats(),
    }


@app.post("/api/admin/model/test")
async def test_model_connection(
    body: Dict[str, Any] = None,
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    """Ping the configured LLM with a simple prompt and return the response.
    Used by Admin UI to confirm the model is reachable after a routing change."""
    if not llm_available():
        return {
            "ok": False,
            "model": get_model() or "none",
            "provider": get_provider() or "none",
            "error": "No LLM configured. Set OPENAI_API_KEY / GEMINI_API_KEY or connect an Ollama model in Admin.",
        }
    custom_prompt = (body or {}).get("prompt") if body else None
    system_msg = "You are AIOC, an AI incident command system. Answer the user's question directly and concisely."
    user_prompt = str(custom_prompt or "Confirm you are online by responding with: AIOC ready.").strip()
    schema = {
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    }
    try:
        import asyncio
        result = await asyncio.wait_for(
            complete_json(system=system_msg, prompt=user_prompt, schema=schema, schema_name="reply"),
            timeout=20.0,
        )
        return {
            "ok": True,
            "model": get_model(),
            "provider": get_provider(),
            "reply": str(result.get("reply", "") or str(result))[:500],
        }
    except Exception as exc:
        return {
            "ok": False,
            "model": get_model(),
            "provider": get_provider(),
            "error": str(exc)[:300],
        }


@app.get("/api/admin/connectors/catalog")
async def get_connector_catalog() -> Dict[str, Any]:
    return {"connectors": CONNECTOR_CATALOG}


@app.get("/api/admin/connectors")
async def get_connectors() -> Dict[str, Any]:
    return {"connectors": list_connectors()}


@app.post("/api/admin/connectors")
async def save_connector(body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return upsert_connector(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/admin/connectors/{connector_id}")
async def remove_connector(connector_id: str) -> Dict[str, Any]:
    if not delete_connector(connector_id):
        raise HTTPException(status_code=404, detail="Connector not found")
    return {"deleted": connector_id}


@app.post("/api/admin/connectors/{connector_id}/heartbeat")
async def connector_heartbeat(connector_id: str) -> Dict[str, Any]:
    try:
        return await heartbeat(connector_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Connector not found") from exc


@app.post("/api/admin/ollama/apply")
async def apply_ollama_model(body: Dict[str, Any], _: None = Depends(require_api_access)) -> Dict[str, Any]:
    """Create or update an Ollama connector record for the selected local model,
    then return the current Ollama health so the UI can reflect availability.

    Expected body: { "model": "qwen2.5:1b" }
    """
    model = str(body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")

    existing_records = runtime_connectors("ollama")
    existing_connector = None
    if existing_records:
        existing_connector = sorted(
            existing_records,
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )[0]

    config: Dict[str, Any] = {"model": model}
    if existing_connector:
        existing_endpoint = str((existing_connector.get("config") or {}).get("endpoint") or "").strip()
        if existing_endpoint:
            config["endpoint"] = existing_endpoint
        connector_id = str(existing_connector.get("id"))
    else:
        connector_id = None

    try:
        payload = {
            "name": f"Ollama ({model})",
            "type": "ollama",
            "enabled": True,
            "config": config,
        }
        if connector_id:
            payload["id"] = connector_id
        record = upsert_connector(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    health = await ollama_health()
    return {
        "connector": {
            k: (v if k != "config" else {kk: (vv if kk != "api_key" else "********") for kk, vv in (v or {}).items()})
            for k, v in record.items()
        },
        "ollama": health,
    }


@app.get("/api/admin/model/providers")
async def list_model_providers(_: None = Depends(require_api_access)) -> Dict[str, Any]:
    """Return available online LLM providers (env + connector) and current selection.

    This lets Admin UI present OpenAI/Gemini/Groq/Claude as selectable runtime providers
    in addition to local Ollama connectors.
    """
    from agents import llm as llm_mod
    from agents.connector_registry import runtime_connectors

    providers = []
    # Environment-based availability
    for provider_name in llm_mod.PROVIDER_DEFAULTS.keys():
        env_keys = llm_mod.PROVIDER_KEY_ENVS.get(provider_name, ())
        has_env = any(bool(os.getenv(k, "").strip()) for k in env_keys)
        default_model = llm_mod.PROVIDER_DEFAULTS.get(provider_name, {}).get("model")
        providers.append({"provider": provider_name, "has_env": has_env, "default_model": default_model})

    # Connector-based records
    connector_records = runtime_connectors(*list(llm_mod.PROVIDER_CONNECTOR_TYPES.keys()))
    connectors = [
        {"id": rec.get("id"), "type": rec.get("type"), "config": {k: ("********" if k == "api_key" else v) for k, v in (rec.get("config") or {}).items()}}
        for rec in connector_records
    ]

    current = llm_mod.get_online_config()
    return {
        "providers": providers,
        "connectors": connectors,
        "current_provider": current.provider if current else None,
        "current_model": current.model if current else None,
    }


@app.post("/api/admin/model/select")
async def select_model_provider(body: Dict[str, Any], _: None = Depends(require_api_access)) -> Dict[str, Any]:
    """Select an online provider at runtime.

    Body options:
    - {"connector_id": "<id>"}  -> select a runtime connector by id
    - {"provider": "openai", "model": "gpt-4o-mini"} -> set env-backed provider and model
    """
    import os as _os
    connector_id = str(body.get("connector_id") or "").strip()
    provider = str(body.get("provider") or "").strip().casefold()
    model = str(body.get("model") or "").strip()

    from agents import llm as llm_mod
    from agents.connector_registry import runtime_connectors, upsert_connector

    # If a connector id is provided, mark that connector as active and
    # deactivate other LLM connectors so selection is durable.
    if connector_id:
        connectors = runtime_connectors(*list(llm_mod.PROVIDER_CONNECTOR_TYPES.keys()), "ollama")
        match = next((c for c in connectors if str(c.get("id")) == connector_id), None)
        if not match:
            raise HTTPException(status_code=404, detail="connector not found")
        # Deactivate other LLM connectors and activate the requested one
        for rec in connectors:
            cfg = dict(rec.get("config") or {})
            cfg["active"] = str(rec.get("id")) == connector_id
            try:
                upsert_connector({"id": rec.get("id"), "type": rec.get("type"), "name": rec.get("name"), "enabled": True, "config": cfg})
            except Exception:
                continue
        _os.environ["ONLINE_LLM_CONNECTOR_ID"] = connector_id

    # If a provider selection is given (env-backed), create or update a connector
    # record for durability and mark it active.
    if provider:
        prov = provider.casefold()
        # support 'ollama' specially
        if prov == "ollama":
            connector_type = "ollama"
            cfg = {"model": model or llm_mod.OLLAMA_DEFAULT_MODEL, "endpoint": os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_URL")}
        else:
            # find connector_type by provider value
            inv = {k: v for k, v in llm_mod.PROVIDER_CONNECTOR_TYPES.items()}
            connector_type = next((k for k, v in inv.items() if v == prov), None)
            if not connector_type:
                raise HTTPException(status_code=400, detail="unsupported provider")
            cfg = {"model": model or llm_mod.PROVIDER_DEFAULTS.get(prov, {}).get("model")}
        # Upsert a connector record for this provider
        try:
            record = upsert_connector({"type": connector_type, "name": f"{connector_type}", "enabled": True, "config": cfg})
            _os.environ["ONLINE_LLM_CONNECTOR_ID"] = record.get("id")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to persist connector: {exc}")

    # Return the newly-selected config
    from agents.llm import get_online_config

    cfg = get_online_config()
    return {"ok": True, "selected": {"provider": cfg.provider if cfg else None, "model": cfg.model if cfg else None, "connector_id": str(cfg.source) if cfg else None}}


# `/api/admin/graph/config` removed — runtime graph backend config is deprecated.


@app.get("/api/kg/health")
async def kg_health(_: None = Depends(require_api_access)) -> Dict[str, Any]:
    """Return simple KG counts for visibility (nodes, edges, backend)."""
    from agents.query_memory import incident_graph_snapshot

    graph = incident_graph_snapshot()
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    backend = graph.get("backend") or "unknown"
    evidence_count = sum(1 for n in nodes if n.get("type") == "evidence")
    incident_count = sum(1 for n in nodes if n.get("type") == "incident")
    return {"backend": backend, "nodes": len(nodes), "edges": len(edges), "incidents": incident_count, "evidence": evidence_count}


@app.get("/api/knowledge-graph/query")
async def knowledge_graph_query(q: str, _: None = Depends(require_api_access)) -> Dict[str, Any]:
    """Simple graph text search across node id/label/type/detail.

    Returns matching nodes and the edges that reference them.
    """
    if not q or not str(q).strip():
        raise HTTPException(status_code=400, detail="q is required")
    # Use the same live data source as /api/knowledge-graph so search always works
    records = _analytics_records(include_synthetic=True)
    graph = knowledge_graph(records)
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    ql = str(q).casefold()
    matched_ids = {n.get("id") for n in nodes if any((str(n.get(k) or "")).casefold().find(ql) != -1 for k in ("id", "label", "type", "detail", "severity"))}
    matched_nodes = [n for n in nodes if n.get("id") in matched_ids]
    matched_edges = [e for e in edges if e.get("source") in matched_ids or e.get("target") in matched_ids]
    return {"query": q, "nodes": matched_nodes, "edges": matched_edges}


@app.get("/api/knowledge-graph/node/{node_id:path}")
async def knowledge_graph_node(node_id: str, _: None = Depends(require_api_access)) -> Dict[str, Any]:
    """Return node details and immediate edges for the given node id."""
    if not node_id:
        raise HTTPException(status_code=400, detail="node_id is required")
    # Use the same live data source as /api/knowledge-graph
    records = _analytics_records(include_synthetic=True)
    graph = knowledge_graph(records)
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    node = next((n for n in nodes if str(n.get("id")) == node_id), None)
    if not node:
        raise HTTPException(status_code=404, detail="node not found")
    related = [e for e in edges if e.get("source") == node_id or e.get("target") == node_id]
    return {"node": node, "edges": related}


@app.delete("/api/admin/query-cache")
async def clear_query_cache(_: None = Depends(require_api_access)) -> Dict[str, Any]:
    return {"invalidated": invalidate_query_memory(), "cache": cache_stats()}


@app.get("/api/analytics/incidents")
async def get_incident_analytics(period: str = "week") -> Dict[str, Any]:
    if period not in {"day", "week", "month"}:
        raise HTTPException(status_code=400, detail="period must be day, week, or month")
    records = _analytics_records_with_history()
    return incident_analytics(records, period)


@app.get("/api/knowledge-graph")
async def get_incident_knowledge_graph() -> Dict[str, Any]:
    records = _analytics_records(include_synthetic=True)
    # Always use the clean knowledge_graph() function which produces flat, React-safe nodes.
    # incident_graph_snapshot returns raw incident dicts with nested objects (blast_radius,
    # owner_team etc.) that crash the React renderer with "Objects are not valid as a React child".
    return knowledge_graph(records)


@app.post("/api/admin/teams/handshake")
async def teams_handshake(_: None = Depends(require_api_access)) -> Dict[str, Any]:
    ok = await post_teams_handshake()
    return {"ok": ok, "configured": teams_configured()}


@app.post("/api/slack/commands")
async def handle_slack_command(
    request: Request,
    x_slack_signature: str = Header(default=""),
    x_slack_request_timestamp: str = Header(default=""),
) -> Dict[str, Any]:
    body, payload = await slack_assistant.read_payload(request)
    await slack_assistant.verify_request(
        body,
        x_slack_signature=x_slack_signature,
        x_slack_request_timestamp=x_slack_request_timestamp,
    )
    command, rest = parse_command(str(payload.get("text", "")))
    channel_id = str(payload.get("channel_id", slack_assistant.default_channel))
    user_id = str(payload.get("user_id", "slack-user"))

    if command in {"help", "h"}:
        return {"response_type": "ephemeral", "text": slack_assistant.home_text()}

    if command in {"trigger", "open", "start"}:
        incident = await _create_incident(
            scenario_from_text(rest),
            slack_context={"channel_id": channel_id, "user_id": user_id},
        )
        return {
            "response_type": "in_channel",
            "text": f"Incident `{incident['incident_id'][:8]}` opened from Slack. Agents are investigating.",
        }

    if command in {"status", "show"}:
        incident_id = rest or (incident_order[0] if incident_order else "")
        if not incident_id:
            return {"response_type": "ephemeral", "text": "No incidents are active yet."}
        incident_id = _resolve_incident_id(incident_id)
        return {"response_type": "ephemeral", "text": summarize_incident(incident_store[incident_id])}

    if command == "ask":
        incident_token, _, question = rest.partition(" ")
        if not incident_token or not question.strip():
            return {
                "response_type": "ephemeral",
                "text": "Usage: `/aioc ask <incident_id> <question>`",
            }
        incident_id = _resolve_incident_id(incident_token)
        payload, source = await answer_question(incident_store[incident_id], question.strip())
        return {"response_type": "ephemeral", "text": f"{payload.get('answer', '')}\n_Source: {source}_"}

    if command == "trace":
        incident_id = _resolve_incident_id(rest or (incident_order[0] if incident_order else ""))
        spans = incident_store[incident_id].get("agent_invocations", [])[-6:]
        text = "\n".join(
            f"â€¢ {span.get('agent')}: {span.get('reasoning') or span.get('action')}"
            for span in spans
        ) or "No agent spans recorded yet."
        return {"response_type": "ephemeral", "text": text}

    return {
        "response_type": "ephemeral",
        "text": f"Unknown AIOC command `{command}`.\n{slack_assistant.home_text()}",
    }


@app.post("/api/slack/events")
async def handle_slack_events(
    request: Request,
    x_slack_signature: str = Header(default=""),
    x_slack_request_timestamp: str = Header(default=""),
) -> Dict[str, Any]:
    body, payload = await slack_assistant.read_payload(request)
    await slack_assistant.verify_request(
        body,
        x_slack_signature=x_slack_signature,
        x_slack_request_timestamp=x_slack_request_timestamp,
    )
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    event = payload.get("event") or {}
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return {"ok": True}
    if event.get("type") in {"app_mention", "message"}:
        asyncio.create_task(_handle_slack_message_event(event))
    return {"ok": True}


@app.post("/api/slack/interactivity")
async def handle_slack_interactivity(
    request: Request,
    x_slack_signature: str = Header(default=""),
    x_slack_request_timestamp: str = Header(default=""),
) -> Dict[str, Any]:
    body, payload = await slack_assistant.read_payload(request)
    await slack_assistant.verify_request(
        body,
        x_slack_signature=x_slack_signature,
        x_slack_request_timestamp=x_slack_request_timestamp,
    )
    actions = payload.get("actions") or []
    if not actions:
        return {"text": "No Slack action found."}
    action = actions[0]
    action_id = str(action.get("action_id", ""))
    incident_id = _resolve_incident_id(str(action.get("value", "")))
    actor = str((payload.get("user") or {}).get("id", "slack-user"))
    record = incident_store[incident_id]

    if action_id == "aioc_accept_rca":
        await authorize_review("review_rca", record, {"actor": actor})
        append_review_event(
            record,
            action="accept_rca",
            actor=f"slack:{actor}",
            decision="accepted",
            reason="Accepted from Slack assistant.",
            previous_value=record.get("root_cause"),
            new_value=record.get("root_cause"),
        )
        record["quality_gates"] = evaluate_quality_gates(record)
        set_lifecycle_after_review(record)
        return {"text": f"RCA accepted for `{incident_id[:8]}`."}

    if action_id == "aioc_request_more_data":
        await authorize_review("request_more_data", record, {"actor": actor})
        append_review_event(
            record,
            action="request_more_data",
            actor=f"slack:{actor}",
            decision="requested",
            reason="Requested from Slack assistant.",
        )
        _start_reinvestigation(incident_id, actor=f"slack:{actor}", reason="Requested from Slack assistant.")
        return {"text": f"Requested another investigation pass for `{incident_id[:8]}`."}

    return {"text": f"Unhandled Slack action `{action_id}`."}


@app.post("/api/incidents/trigger")
async def trigger_incident(
    incident_data: Dict[str, Any],
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    return await _create_incident(incident_data)


@app.get("/api/incidents")
async def list_incidents() -> List[Dict[str, Any]]:
    return [incident_store[iid] for iid in incident_order if iid in incident_store]


@app.post("/api/incidents/reset")
async def reset_incidents(
    body: Dict[str, Any],
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    """Clear the in-memory demo queue.

    This app intentionally uses an in-memory incident store for local demos.
    The reset endpoint gives the Next.js command center a safe way to remove
    duplicated demo runs without pretending they are separate live incidents.
    """
    if body.get("confirm") != "RESET_DEMO_INCIDENTS":
        raise HTTPException(status_code=400, detail="confirmation token is required")
    cleared_count: int = len(incident_store)
    incident_store.clear()
    incident_order.clear()
    reinvestigation_jobs.clear()
    _save_reinvestigation_jobs()
    return {"cleared": cleared_count}


@app.get("/api/incidents/{incident_id}")
async def get_incident(incident_id: str) -> Dict[str, Any]:
    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")

    return incident_store[incident_id]


@app.get("/api/incidents/{incident_id}/trace")
async def get_incident_trace(incident_id: str) -> Dict[str, Any]:
    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")
    record: Dict[str, Any] = incident_store[incident_id]
    return {
        "incident_id": record.get("incident_id"),
        "trace_id": record.get("trace_id"),
        "lifecycle_status": record.get("lifecycle_status"),
        "agent_status": record.get("agent_status"),
        "quality_gates": record.get("quality_gates", {}),
        "compact_contexts": record.get("compact_contexts", []),
        "review_events": record.get("review_events", []),
        "spans": record.get("agent_invocations", []),
    }


@app.post("/api/incidents/{incident_id}/ask")
async def ask_incident(
    incident_id: str,
    body: Dict[str, Any],
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    """Natural-language Q&A grounded in one incident's investigation data."""
    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")
    question: str = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    payload, source = await answer_question(
        incident_store[incident_id],
        question,
        language_code=str(body.get("language_code", "")).strip() or None,
    )
    return _normalize_assistant_response(payload, source)


def _build_global_jarvis_context(question: str) -> Dict[str, Any]:
    """Comprehensive platform intelligence brief injected into every Jarvis LLM call."""
    all_records = _analytics_records_with_history()
    for r in all_records:
        if not r.get("service_profile"):
            enrich_record(r)

    live = [r for r in all_records if not r.get("historical_baseline") and not r.get("synthetic")]
    resolved = [r for r in all_records if str(r.get("lifecycle_status", "")) == "resolved"]
    active = [r for r in live if str(r.get("current_status", "")) not in {"complete", "resolved"}]

    sev_counts: Dict[str, int] = {}
    svc_counts: Dict[str, int] = {}
    total_users = 0
    total_impact = 0.0
    for r in all_records:
        sev = str(r.get("severity", "unknown")).lower()
        svc = str(r.get("service", "unknown"))
        sev_counts[sev] = sev_counts.get(sev, 0) + 1
        svc_counts[svc] = svc_counts.get(svc, 0) + 1
        total_users += int(r.get("affected_users") or 0)
        total_impact += float(r.get("estimated_revenue_impact_per_minute") or 0.0)

    top_services = sorted(svc_counts.items(), key=lambda x: x[1], reverse=True)[:6]

    def _brief(r: Dict[str, Any]) -> Dict[str, Any]:
        rc = r.get("root_cause") or {}
        steps = (r.get("recovery_recommendations") or [])[:3]
        return {
            "incident_id": str(r.get("incident_id", ""))[:12],
            "service": r.get("service"),
            "severity": r.get("severity"),
            "description": r.get("alert_description"),
            "root_cause": rc.get("hypothesis"),
            "rca_confidence": rc.get("confidence"),
            "resolution_steps": "; ".join(steps) if steps else "see runbook",
            "affected_users": r.get("affected_users"),
            "revenue_impact_per_min": r.get("estimated_revenue_impact_per_minute"),
            "status": r.get("lifecycle_status") or r.get("current_status"),
            "date": str(r.get("created_at", ""))[:10],
            "owner": (r.get("ownership") or {}).get("primary") or (r.get("service_profile") or {}).get("owner_primary"),
        }

    relevant = _matching_incident_records(question)[:5]
    recent_resolved = sorted(resolved, key=lambda r: str(r.get("created_at") or ""), reverse=True)[:6]

    catalog_svcs = sorted({str(r.get("service")) for r in all_records if r.get("service")})
    service_catalog = []
    for svc in catalog_svcs[:12]:
        owner = next(
            ((r.get("ownership") or {}).get("primary") or (r.get("service_profile") or {}).get("owner_primary")
             for r in all_records if r.get("service") == svc and ((r.get("ownership") or {}).get("primary") or (r.get("service_profile") or {}).get("owner_primary"))),
            None,
        )
        service_catalog.append({"service": svc, "owner": owner, "incident_count": svc_counts.get(svc, 0)})

    return {
        "platform_summary": {
            "total_incidents_30d": len(all_records),
            "active_incidents": len(active),
            "resolved_incidents": len(resolved),
            "total_affected_users_30d": total_users,
            "total_revenue_exposure_per_min_30d": round(total_impact, 2),
            "severity_distribution": sev_counts,
            "top_affected_services": [{"service": s, "count": c} for s, c in top_services],
        },
        "active_incidents": [_brief(r) for r in active[:5]],
        "recently_resolved_incidents": [_brief(r) for r in recent_resolved],
        "query_relevant_incidents": [_brief(r) for r in relevant],
        "service_catalog": service_catalog,
        "capabilities": (
            "I am Jarvis — global agentic incident intelligence. I answer questions about active/historical "
            "incidents, root causes, resolutions, service ownership, impact metrics, MTTR trends, and patterns."
        ),
    }





@app.post("/api/assistant/chat")
async def assistant_chat(
    body: Dict[str, Any],
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    import asyncio

    question: str = str(body.get("message", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="message is required")
    incident_id: str = str(body.get("incident_id", "")).strip()
    language_code: str | None = str(body.get("language_code", "")).strip() or None

    async def _do_answer() -> Dict[str, Any]:
        # --- INCIDENT-SCOPED QUERY (specific incident_id given) ---
        if incident_id:
            if incident_id not in incident_store:
                raise HTTPException(status_code=404, detail="Incident not found")
            record = incident_store[incident_id]
            if _looks_like_resolution_question(question):
                return _normalize_assistant_response(
                    {
                        "answer": _resolution_text(record),
                        "confidence": round(float((record.get("root_cause") or {}).get("confidence") or record.get("rca_confidence") or 0.72), 3),
                        "citations": [_resolution_citation(record)],
                        "follow_ups": [
                            "Should I open the agent journey for this resolution?",
                            "Do you want the rollback and safety checks too?",
                        ],
                        "language": language_code or "en",
                    },
                    "incident_resolution",
                )
            payload, source = await answer_question(
                record,
                question,
                language_code=language_code,
            )
            payload.setdefault("knowledge", build_knowledge_context(question, incident_context=record))
            # Persist retrieved knowledge as graph evidence for later citations.
            try:
                rec = record
                def _persist():
                    try:
                        upsert_knowledge_evidence(question, payload.get("knowledge") or build_knowledge_context(question, incident_context=record), record=rec)
                    except Exception:
                        pass
                await asyncio.get_event_loop().run_in_executor(None, _persist)
            except Exception as exc:
                print(f"[jarvis] failed to persist incident evidence: {exc}")
            return _normalize_assistant_response(payload, source)

        # --- GLOBAL JARVIS QUERY: build comprehensive platform context ---
        # Heuristic lookups enrich context — they do NOT short-circuit the LLM.
        # The LLM always produces the final answer with full platform intelligence.
        try:
            global_ctx = _build_global_jarvis_context(question)
        except Exception as ctx_err:
            print(f"[jarvis] context build failed: {ctx_err}")
            global_ctx = {}

        # Merge: base jarvis context + heuristic resolution data (if available)
        resolution_data = _answer_incident_resolution_lookup(question)
        if resolution_data:
            # Inject pre-computed resolution detail into context for LLM to use
            global_ctx["pre_resolved_answer"] = {
                "heuristic_answer": resolution_data.get("answer", ""),
                "citations": resolution_data.get("citations", []),
            }

        knowledge_context = build_knowledge_context(question)
        payload, source = await answer_question(
            {},
            question,
            language_code=language_code,
            system_context=global_ctx,
        )
        # Attach heuristic citation data if LLM didn't source its own
        if resolution_data and not payload.get("citations"):
            payload["citations"] = resolution_data.get("citations", [])
        payload.setdefault("knowledge", knowledge_context)
        # Persist retrieved knowledge as graph evidence for global queries as well.
        try:
            def _persist_global():
                try:
                    upsert_knowledge_evidence(question, payload.get("knowledge") or knowledge_context, record=None)
                except Exception:
                    pass
            await asyncio.get_event_loop().run_in_executor(None, _persist_global)
        except Exception as exc:
            print(f"[jarvis] failed to persist global evidence: {exc}")
        return _normalize_assistant_response(payload, source)

    try:
        return await asyncio.wait_for(_do_answer(), timeout=35.0)
    except asyncio.TimeoutError:
        # Graceful heuristic fallback when LLM is completely unavailable
        try:
            heuristic = _answer_incident_resolution_lookup(question) or _answer_incident_lookup(question)
        except Exception:
            heuristic = None
        if heuristic:
            return _normalize_assistant_response(heuristic, "timeout:heuristic_resolution")
        live = [incident_store[k] for k in list(incident_store.keys())[-3:]]
        count = len(incident_store)
        service_list = ", ".join({str(item.get("service", "?")) for item in live}) or "various services"
        return _normalize_assistant_response(
            {
                "answer": (
                    f"The system is currently tracking {count} incident(s) across {service_list}. "
                    "The LLM response timed out — try a more specific question or check your model is configured in Admin."
                ),
                "confidence": 0.3,
                "citations": [],
                "follow_ups": [
                    "Which incident do you want to investigate?",
                    "Ask me about root cause, impact, or recommended fixes for a specific service.",
                ],
                "language": language_code or "en",
            },
            "timeout:heuristic",
        )
    except HTTPException:
        raise
    except Exception as exc:
        return _normalize_assistant_response(
            {
                "answer": f"An error occurred while answering: {str(exc)[:200]}. Please check the model configuration in Admin.",
                "confidence": 0.0,
                "citations": [],
                "follow_ups": ["Try rephrasing your question.", "Check Admin → Model routing is configured."],
                "language": language_code or "en",
            },
            "error:heuristic",
        )




@app.post("/api/knowledge/upload")
async def upload_knowledge(
    file: UploadFile | None = File(None),
    text: str = Form(""),
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    if not file and not text.strip():
        raise HTTPException(status_code=400, detail="file or text is required")

    if file:
        try:
            raw = await file.read()
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Uploaded file must be UTF-8 text.")
        filename = file.filename or "uploaded.txt"
    else:
        content = text.strip()
        filename = f"manual-{uuid.uuid4().hex}.txt"

    chunk = insert_uploaded_document(content=content, filename=filename)
    return {
        "uploaded": True,
        "chunk_id": chunk.chunk_id,
        "source_path": chunk.source_path,
        "title": chunk.title,
    }


@app.get("/api/knowledge/search")
async def knowledge_search(
    q: str,
    incident_id: str = "",
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    if not q.strip():
        raise HTTPException(status_code=400, detail="q is required")
    incident_context = incident_store.get(incident_id) if incident_id and incident_id in incident_store else None
    hits = search_knowledge(q, incident_context=incident_context)
    return {
        "query": q,
        "results": [
            {
                "title": hit.title,
                "source_path": hit.source_path,
                "kind": hit.kind,
                "content": hit.content,
                "score": round(hit.score, 3),
                "citation": hit.citation,
            }
            for hit in hits
        ],
        "confidence": round(max((hit.score for hit in hits), default=0.0), 3),
    }


@app.post("/api/incidents/{incident_id}/remediation/{step_index}/decision")
async def decide_remediation(
    incident_id: str,
    step_index: int,
    body: Dict[str, Any],
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    """Human-in-the-loop gate: no recovery action is considered actionable
    until a human explicitly approves it."""
    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")
    decision: str = str(body.get("decision", ""))
    if decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'rejected'")
    record: Dict[str, Any] = incident_store[incident_id]
    if step_index < 0 or step_index >= len(record.get("recovery_recommendations", [])):
        raise HTTPException(status_code=404, detail="Recommendation not found")
    await authorize_review("remediation_decision", record, body)
    policy = evaluate_remediation_policy(record, step_index, body)
    if decision == "approved" and not policy.get("allowed"):
        record.setdefault("remediation_policy_events", []).append(policy)
        raise HTTPException(status_code=409, detail={"message": "remediation blocked by safety policy", "policy": policy})
    record.setdefault("remediation_decisions", {})[str(step_index)] = {
        "decision": decision,
        "decided_at": datetime.now().isoformat(),
        "policy": policy,
    }
    record["remediation_policy"] = policy
    append_review_event(
        record,
        action="remediation_decision",
        actor=str(body.get("actor", "anonymous")),
        decision=decision,
        reason=str(body.get("reason", "")),
        new_value={"step_index": step_index, "decision": decision},
    )
    record["quality_gates"] = evaluate_quality_gates(record)
    set_lifecycle_after_review(record)
    if decision == "approved" and policy.get("risk_level") == "high":
        await post_high_risk_email_alert(
            record,
            f"High-risk remediation approved for incident {incident_id}, step {step_index}",
        )
        record["high_risk_email_alert_sent"] = True
    return record


@app.post("/api/incidents/{incident_id}/resume")
async def resume_incident_graph(
    incident_id: str,
    body: Dict[str, Any] = None,
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    """Resume a graph that was paused at the human_approval interrupt node.

    Call this endpoint after all remediation decisions have been recorded via
    POST /api/incidents/{incident_id}/remediation/{step}/decision. The graph
    resumes from the SQLite checkpoint, runs human_approval → learning → END.
    """
    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")
    record = incident_store[incident_id]
    if record.get("lifecycle_status") not in {"needs_human_review"}:
        raise HTTPException(
            status_code=409,
            detail="Incident is not waiting for human review. Nothing to resume.",
        )

    async def _resume() -> None:
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            import aiosqlite
            ckp_path = Path("data/checkpoints.sqlite3")
            ckp_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(str(ckp_path)) as conn:
                checkpointer = AsyncSqliteSaver(conn)
                graph = create_incident_analysis_graph(checkpointer=checkpointer)
                config = {"configurable": {"thread_id": incident_id}, "recursion_limit": 80}
                async for values in graph.astream(None, config=config, stream_mode="values"):
                    if not isinstance(values, dict):
                        values = dict(vars(values))
                    prev = incident_store.get(incident_id, {})
                    rec = _serialize_state(values)
                    enrich_record(rec)
                    rec["created_at"] = prev.get("created_at")
                    if prev.get("remediation_decisions"):
                        rec["remediation_decisions"] = prev["remediation_decisions"]
                    incident_store[incident_id] = rec
                incident_store[incident_id]["lifecycle_status"] = "resolved"
                incident_store[incident_id]["current_status"] = "complete"
                incident_memory_agent(incident_store[incident_id])
        except ImportError:
            # langgraph-checkpoint-sqlite not installed — fall back to flag-only
            incident_store[incident_id]["lifecycle_status"] = "resolved"
            incident_store[incident_id]["current_status"] = "complete"
        except Exception as exc:
            print(f"[resume] Error resuming graph for {incident_id}: {exc}")
            incident_store[incident_id]["lifecycle_status"] = "resolved"

    asyncio.create_task(_resume())
    return {"status": "resuming", "incident_id": incident_id}


@app.post("/api/incidents/{incident_id}/review/rca")
async def review_rca(
    incident_id: str,
    body: Dict[str, Any],
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")
    decision: str = str(body.get("decision", "")).strip()
    if decision not in ("accepted", "rejected", "evidence_insufficient"):
        raise HTTPException(
            status_code=400,
            detail="decision must be accepted, rejected, or evidence_insufficient",
        )
    record: Dict[str, Any] = incident_store[incident_id]
    await authorize_review("review_rca", record, body)
    await authorize_review("remediation_decision", record, body)
    append_review_event(
        record,
        action="accept_rca" if decision == "accepted" else "reject_rca",
        actor=str(body.get("actor", "anonymous")),
        decision=decision,
        reason=str(body.get("reason", "")),
        previous_value=record.get("root_cause"),
        new_value=record.get("root_cause"),
    )
    record["quality_gates"] = evaluate_quality_gates(record)
    set_lifecycle_after_review(record)
    return record


@app.post("/api/incidents/{incident_id}/review/request-more-data")
async def review_request_more_data(
    incident_id: str,
    body: Dict[str, Any],
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")
    record: Dict[str, Any] = incident_store[incident_id]
    await authorize_review("request_more_data", record, body)
    append_review_event(
        record,
        action="request_more_data",
        actor=str(body.get("actor", "anonymous")),
        decision="requested",
        reason=str(body.get("reason", "")),
    )
    record["current_status"] = "review_requested_more_data"
    record["agent_status"] = "review_requested_more_data"
    record["lifecycle_status"] = "investigating"
    _start_reinvestigation(
        incident_id,
        actor=str(body.get("actor", "anonymous")),
        reason=str(body.get("reason", "")),
    )
    return record


@app.post("/api/incidents/{incident_id}/review/override-root-cause")
async def override_root_cause(
    incident_id: str,
    body: Dict[str, Any],
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")
    hypothesis: str = str(body.get("hypothesis", "")).strip()
    if not hypothesis:
        raise HTTPException(status_code=400, detail="hypothesis is required")
    record: Dict[str, Any] = incident_store[incident_id]
    await authorize_review("override_root_cause", record, body)
    previous: Dict[str, Any] = dict(record.get("root_cause") or {})
    root_cause: Dict[str, Any] = dict(previous)
    root_cause["hypothesis"] = hypothesis
    root_cause["review_override"] = True
    root_cause["review_override_reason"] = str(body.get("reason", ""))
    record["root_cause"] = root_cause
    append_review_event(
        record,
        action="override_root_cause",
        actor=str(body.get("actor", "anonymous")),
        decision="overridden",
        reason=str(body.get("reason", "")),
        previous_value=previous,
        new_value=root_cause,
    )
    record["quality_gates"] = evaluate_quality_gates(record)
    set_lifecycle_after_review(record)
    return record


def _postmortem_markdown(record: Dict[str, Any]) -> str:
    rc: Dict[str, Any] = record.get("root_cause") or {}
    decisions: Dict[str, Any] = record.get("remediation_decisions", {})
    impact: Dict[str, Any] = record.get("revenue_impact_justification") or {}
    log_cache: Dict[str, Any] = record.get("log_context_cache") or {}
    lines: List[str] = [
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
        record.get("executive_summary") or "N/A",
        "",
        "## Root Cause",
        "",
        f"**{rc.get('hypothesis', 'Unknown')}** (confidence: {rc.get('confidence', 0) * 100:.0f}%)",
        "",
    ]
    if rc.get("deploy_correlation"):
        lines += [f"> âš¡ {rc['deploy_correlation']}", ""]
    lines += ["### Supporting Evidence", ""]
    lines += [f"- {e}" for e in rc.get("supporting_evidence", [])]
    if rc.get("supporting_evidence_refs"):
        lines += ["", "### Evidence References", ""]
        lines += [
            f"- `{ref.get('evidence_id')}` ({ref.get('evidence_type')}): {ref.get('claim')}"
            for ref in rc.get("supporting_evidence_refs", [])
        ]
    if rc.get("confidence_breakdown"):
        lines += ["", "### Confidence Breakdown", ""]
        lines += [
            f"- {key}: {value}"
            for key, value in rc.get("confidence_breakdown", {}).items()
        ]
    if rc.get("ruled_out_hypotheses"):
        lines += ["", "### Alternatives Considered & Ruled Out", ""]
        lines += [
            f"- ~~{r.get('hypothesis')}~~ â€” {r.get('reason')}"
            for r in rc["ruled_out_hypotheses"]
        ]
    lines += [
        "",
        "## Business Impact",
        "",
        f"- Affected users: {record.get('affected_users', 0):,}",
        f"- Estimated revenue impact: ${record.get('estimated_revenue_impact_per_minute', 0):.2f}/minute",
        f"- Estimated cost impact: ${record.get('estimated_cost_impact_per_minute', 0):.2f}/minute",
        f"- Business risk level: {record.get('business_risk_level', 'unknown')}",
    ]
    if impact:
        lines += [
            (
                f"- Justification: {impact.get('affected_users', 0):,} affected users x "
                f"${impact.get('revenue_per_user_per_minute', 0):.2f}/user/min"
            ),
            (
                f"- Bounded range: ${impact.get('lower_bound_per_minute', 0):.2f}-"
                f"${impact.get('upper_bound_per_minute', 0):.2f}/minute"
            ),
            (
                f"- Limit: impact rate capped at {impact.get('limits', {}).get('impact_rate_ceiling', 1.0):.0%}; "
                f"affected users capped at {impact.get('limits', {}).get('affected_users_ceiling', 0):,}"
            ),
        ]
    if record.get("stakeholder_updates"):
        lines += ["", "## Stakeholder Updates", ""]
        lines += [
            f"- Engineering: {record['stakeholder_updates'].get('engineering', '')}",
            f"- Business: {record['stakeholder_updates'].get('business', '')}",
            f"- Customers: {record['stakeholder_updates'].get('customers', '')}",
            f"- Ops: {record['stakeholder_updates'].get('ops', '')}",
        ]
    if record.get("troubleshooting_plan"):
        lines += ["", "## Troubleshooting Plan", ""]
        lines += [f"- {step}" for step in record.get("troubleshooting_plan", [])]
    if record.get("kpi_guardrails"):
        lines += ["", "## KPI Guardrails", ""]
        guardrails = record.get("kpi_guardrails", {})
        lines += [
            f"- Risk level: {guardrails.get('risk_level', 'unknown')}",
            *[f"- {item}" for item in guardrails.get("operational_guardrails", [])],
            *[f"- {item}" for item in guardrails.get("business_guardrails", [])],
        ]
    if log_cache:
        lines += [
            "",
            "## Centralized Log Context",
            "",
            f"- Logs scanned: {log_cache.get('total_logs_scanned', 0):,}",
            f"- Error context windows cached: {len(log_cache.get('error_contexts', []))}",
        ]
        for item in log_cache.get("hierarchy", []):
            lines.append(
                f"- {item.get('severity')} / {item.get('type')}: {item.get('count')} events"
            )
    lines += ["", "## Recovery Actions", ""]
    for i, rec in enumerate(record.get("recovery_recommendations", [])):
        status: str = decisions.get(str(i), {}).get("decision", "pending review")
        lines.append(f"{i + 1}. {rec} â€” _{status}_")
    if record.get("review_events"):
        lines += ["", "## Human Review Events", ""]
        lines += [
            f"- `{str(event.get('timestamp', ''))[11:19]}` {event.get('actor')} "
            f"{event.get('action')} -> {event.get('decision')}: {event.get('reason', '')}"
            for event in record.get("review_events", [])
        ]
    if record.get("quality_gates"):
        lines += ["", "## Quality Gates", ""]
        lines += [
            f"- {key}: {value}"
            for key, value in record.get("quality_gates", {}).items()
        ]
    if record.get("similar_incidents"):
        lines += ["", "## Related Past Incidents", ""]
        lines += [
            f"- Incident #{s.get('number')} on {s.get('service')} "
            f"({str(s.get('resolved_at', ''))[:10]}): {s.get('hypothesis')} â€” {s.get('match_reason')}"
            for s in record["similar_incidents"]
        ]
    lines += ["", "## Investigation Timeline", ""]
    for inv in record.get("agent_invocations", []):
        detail: str = inv.get("reasoning") or inv.get("hypothesis") or inv.get("action", "")
        lines.append(
            f"- `{str(inv.get('timestamp', ''))[11:19]}` **{inv.get('agent')}** "
            f"(`{inv.get('span_id', '')}`) â€” {detail}"
        )
    lines += ["", "---", "", "_Generated automatically by AI Operations Command Center_", ""]
    return "\n".join(lines)



@app.get("/api/scenarios")
async def list_demo_scenarios() -> Dict[str, Any]:
    scenarios = [
        {"id": "db", "title": "DB Pool Exhaustion", "service": "payment-api", "description": "Checkout failures from exhausted database connections."},
        {"id": "memory", "title": "Memory Leak", "service": "order-processor", "description": "GC pauses and worker restarts from heap growth."},
        {"id": "timeout", "title": "Timeout Cascade", "service": "checkout-gateway", "description": "Downstream timeouts and retry amplification."},
        {"id": "catalog", "title": "Catalog Migration", "service": "catalog-api", "description": "Schema migration causing catalog API 500s."},
        {"id": "search", "title": "Cache Stampede", "service": "search-api", "description": "Cache miss storm causing search latency."},
    ]
    return {"scenarios": scenarios}


@app.get("/api/incidents/{incident_id}/journey")
async def get_incident_journey(incident_id: str) -> Dict[str, Any]:
    record = incident_store.get(incident_id)
    if not record:
        record = next((item for item in _synthetic_incidents() if item.get("incident_id") == incident_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="Incident not found")
    return {"incident_id": incident_id, "journey": _agent_journey(record), "resolution": _resolution_answer_for_record(record)}


def _parse_raw_log_to_entries(content: str) -> List[Dict[str, Any]]:
    entries = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Try matching standard format: 2026-07-18T14:07:55.211Z WARN  [aws-health] ...
        match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s+([A-Z]+)\s+\[([^\]]+)\]\s+(.*)$", line)
        if match:
            ts, lvl, comp, msg = match.groups()
            entries.append({
                "timestamp": ts,
                "level": lvl,
                "component": comp,
                "message": f"[{comp}] {msg}",
                "evidence_id": f"evt-{uuid.uuid4().hex[:6]}"
            })
        else:
            # Fallback regex for simpler logs
            match2 = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s+([A-Z]+)\s+(.*)$", line)
            if match2:
                ts, lvl, msg = match2.groups()
                entries.append({
                    "timestamp": ts,
                    "level": lvl,
                    "message": msg,
                    "evidence_id": f"evt-{uuid.uuid4().hex[:6]}"
                })
            else:
                entries.append({
                    "timestamp": datetime.now().isoformat(),
                    "level": "INFO",
                    "message": line,
                    "evidence_id": f"evt-{uuid.uuid4().hex[:6]}"
                })
    return entries


@app.post("/api/incidents/upload-log")
async def upload_log_for_analysis(
    request: Request,
    file: UploadFile | None = File(None),
    text: str = Form(""),
    source_connector_id: str | None = Form(None),
    _: None = Depends(require_api_access),
) -> Dict[str, Any]:
    json_text = ""
    if not file and not text.strip():
        try:
            body = await request.json()
            json_text = str((body or {}).get("text") or (body or {}).get("content") or "")
        except Exception:
            json_text = ""
    if not file and not text.strip() and not json_text.strip():
        raise HTTPException(status_code=400, detail="file or text is required")
    if file:
        raw = await file.read()
        try:
            content = raw.decode("utf-8", errors="replace")
        except Exception:
            content = str(raw[:10000])
        filename = file.filename or "uploaded.log"
    else:
        content = text or json_text
        filename = "pasted-log.txt"

    # 1. Parse raw log to structured entries
    raw_entries = _parse_raw_log_to_entries(content)

    # 2. Parse initial parsed details (service, severity, initial RCA guess etc.)
    initial_record = _parse_uploaded_log(content, filename)

    # 3. Construct real IncidentState for background agentic analysis
    incident_id = initial_record["incident_id"]
    state = IncidentState(
        incident_id=incident_id,
        timestamp=initial_record["timestamp"],
        alert_description=initial_record["alert_description"],
        service=initial_record["service"],
        severity=initial_record["severity"],
    )
    state.trace_id = filename
    state.raw_logs = raw_entries
    state.root_cause = initial_record["root_cause"]

    # 4. Serialize initial record status
    record = _serialize_state(dict(vars(state)))
    enrich_record(record)
    record["context_metadata"] = build_incident_context(record)
    
    # Set status as active investigation
    record["current_status"] = "investigating"
    record["agent_status"] = "investigating"
    record["lifecycle_status"] = "investigating"
    record["created_at"] = datetime.now().isoformat()
    if source_connector_id:
        record["source_connector_id"] = source_connector_id
        record["alert_description"] = f"Uploaded log analysis from connector {source_connector_id}: {filename}"

    incident_store[incident_id] = record
    incident_order.insert(0, incident_id)
    upsert_incident_graph(record)
    insert_uploaded_document(content=content[:50000], filename=filename)

    # Surface uploaded logs as operational knowledge so they appear as evidence
    # in the graph (memory or remote backend).
    try:
        upsert_operational_incident_knowledge(record)
    except Exception as exc:
        print(f"[app] warning: failed to upsert operational knowledge: {exc}")

    # 5. Dispatch the background LangGraph orchestrator!
    asyncio.create_task(_run_analysis(incident_id, state))

    return {"uploaded": True, "incident": record, "journey": _agent_journey(record), "resolution": _resolution_answer_for_record(record)}



@app.get("/api/incidents/{incident_id}/resolution")
async def get_incident_resolution(incident_id: str) -> Dict[str, Any]:
    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")
    return _resolution_answer_for_record(incident_store[incident_id])


@app.get("/api/incidents/{incident_id}/postmortem")
async def download_postmortem(incident_id: str) -> Response:
    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")
    markdown: str = _postmortem_markdown(incident_store[incident_id])
    return Response(
        content=markdown,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="postmortem-{incident_id[:8]}.md"'
        },
    )


@app.get("/")
async def serve_dashboard(request: Request) -> Response:
    return await _proxy_web(request, "")


@app.get("/admin")
@app.get("/admin/")
async def serve_admin(request: Request) -> Response:
    return await _proxy_web(request, "admin")


@app.get("/admin/{path:path}")
async def serve_admin_subpath(request: Request, path: str) -> Response:
    return await _proxy_web(request, f"admin/{path}")


@app.get("/incident/{incident_id}")
async def serve_incident_detail(request: Request, incident_id: str) -> Response:
    return await _proxy_web(request, f"incident/{incident_id}")


@app.get("/incident")
@app.get("/incident/")
async def serve_incident_index(request: Request) -> Response:
    return await _proxy_web(request, "incident")


@app.get("/api/incidents/{incident_id}/stream")
async def stream_incident_updates(incident_id: str):
    """SSE endpoint streaming incident state snapshots when they change.

    Clients should open an EventSource to receive a JSON payload for
    the incident each time the server-side `incident_store` is updated for
    that incident (LangGraph node callback writes snapshots there).
    """
    import hashlib
    from json import dumps
    import asyncio

    if incident_id not in incident_store:
        raise HTTPException(status_code=404, detail="Incident not found")

    async def event_stream():
        last_hash = ""
        while True:
            record = incident_store.get(incident_id)
            if not record:
                await asyncio.sleep(0.5)
                continue
            payload = dumps(record)
            cur_hash = hashlib.md5(payload.encode("utf-8")).hexdigest()
            if cur_hash != last_hash:
                last_hash = cur_hash
                yield f"data: {payload}\n\n"
            await asyncio.sleep(0.8)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/pipelines/health")
async def get_pipelines_health() -> Dict[str, Any]:
    """Return the current demo pipeline health states."""
    return {k: v for k, v in pipeline_store.items()}


@app.post("/api/pipelines/simulate")
async def simulate_pipelines(body: Dict[str, Any] = None) -> Dict[str, Any]:
    """Trigger a one-off simulation for the given pipeline or all pipelines.

    Body: {"pipeline": "datadog"} or {"pipeline": "all"} (default: all)
    """
    body = body or {}
    target = (body.get("pipeline") or "all").lower()
    results: Dict[str, Any] = {}
    if target == "all":
        for p in list(pipeline_store.keys()):
            results[p] = await _simulate_pipeline_once(p)
    else:
        if target not in pipeline_store:
            raise HTTPException(status_code=404, detail="Unknown pipeline")
        results[target] = await _simulate_pipeline_once(target)
    return results


@app.get("/api/pipelines/simulate")
async def simulate_pipelines_get(pipeline: str | None = None) -> Dict[str, Any]:
    """Allow GET-based simulation for environments where POST is blocked by proxies.

    Query param: ?pipeline=datadog or ?pipeline=all
    """
    target = (pipeline or "all").lower()
    results: Dict[str, Any] = {}
    if target == "all":
        for p in list(pipeline_store.keys()):
            results[p] = await _simulate_pipeline_once(p)
    else:
        if target not in pipeline_store:
            raise HTTPException(status_code=404, detail="Unknown pipeline")
        results[target] = await _simulate_pipeline_once(target)
    return results


@app.get("/api/pipelines/simulator/status")
async def pipeline_simulator_status() -> Dict[str, Any]:
    return {"running": _pipeline_simulator_task is not None and not _pipeline_simulator_task.done()}


@app.post("/api/pipelines/simulator")
async def control_pipeline_simulator(body: Dict[str, Any]):
    """Control the background pipeline simulator: {"action": "start"|"stop"}.

    Returns current running state.
    """
    global _pipeline_simulator_task
    action = (body or {}).get("action")
    if action not in ("start", "stop"):
        raise HTTPException(status_code=400, detail="action must be 'start' or 'stop'")
    if action == "start":
        if _pipeline_simulator_task is None or _pipeline_simulator_task.done():
            _pipeline_simulator_task = asyncio.create_task(_pipeline_simulator_loop(10.0))
        return {"running": True}
    else:
        if _pipeline_simulator_task is not None:
            _pipeline_simulator_task.cancel()
            _pipeline_simulator_task = None
        return {"running": False}


async def _simulate_pipeline_once(name: str) -> Dict[str, Any]:
    """Simulate a single pipeline fetch/check and update `pipeline_store`."""
    import time
    now = datetime.now().isoformat()
    # Random success/failure for demo purposes
    ok = random.random() > 0.12
    latency = round(random.uniform(30, 650), 1) if ok else round(random.uniform(700, 3500), 1)
    error_rate = round(0.0 if ok else random.uniform(0.01, 0.2), 3)
    status = "ok" if ok else "failed"
    message = "metrics fetched" if ok else "connection timeout"
    record = {
        "name": name,
        "status": status,
        "last_checked": now,
        "latency_ms": latency,
        "error_rate": error_rate,
        "message": message,
    }
    pipeline_store[name] = record
    # If failure, and not already alerted, create a demo incident
    try:
        alerted = _pipeline_alerted.get(name, False)
        if status != "ok" and not alerted:
            _pipeline_alerted[name] = True
            # Create a lightweight incident describing the pipeline failure
            incident_payload = {
                "timestamp": now,
                "service": f"pipeline-{name}",
                "alert_description": f"Pipeline {name} failed: {message} (latency {latency} ms, error_rate {error_rate})",
                "severity": "critical",
            }
            # fire-and-forget: start incident analysis in background
            asyncio.create_task(_create_incident(incident_payload))
        # If recovered, clear alerted flag and post a war room note
        if status == "ok" and _pipeline_alerted.get(name):
            _pipeline_alerted[name] = False
            asyncio.create_task(post_war_room(f"✅ Pipeline {name} recovered: latency {latency} ms"))
    except Exception:
        pass
    return record


async def _pipeline_simulator_loop(interval: float = 10.0):
    """Background loop that simulates pipeline checks periodically."""
    try:
        while True:
            for p in list(pipeline_store.keys()):
                await _simulate_pipeline_once(p)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        return


@app.on_event("startup")
async def _start_pipeline_simulator():
    """Start background simulator when the app starts (demo only)."""
    global _pipeline_simulator_task
    enable = os.getenv("ENABLE_PIPELINE_SIM", "true").lower() in ("1", "true", "yes")
    if enable and _pipeline_simulator_task is None:
        _pipeline_simulator_task = asyncio.create_task(_pipeline_simulator_loop(10.0))


@app.on_event("shutdown")
async def _stop_pipeline_simulator():
    global _pipeline_simulator_task
    if _pipeline_simulator_task is not None:
        _pipeline_simulator_task.cancel()
        _pipeline_simulator_task = None


@app.api_route("/_next/{path:path}", methods=["GET", "HEAD"])
async def serve_next_assets(request: Request, path: str) -> Response:
    return await _proxy_web(request, f"_next/{path}")


@app.api_route("/manifest.json", methods=["GET", "HEAD"])
async def serve_manifest(request: Request) -> Response:
    return await _proxy_web(request, "manifest.json")


@app.api_route("/sw.js", methods=["GET", "HEAD"])
async def serve_service_worker(request: Request) -> Response:
    return await _proxy_web(request, "sw.js")


@app.api_route("/icons/{path:path}", methods=["GET", "HEAD"])
async def serve_icon_assets(request: Request, path: str) -> Response:
    return await _proxy_web(request, f"icons/{path}")


# KG demo page removed — frontend prototype no longer served.


@app.api_route("/{path:path}", methods=["GET", "HEAD"])
async def serve_web_app(request: Request, path: str) -> Response:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")
    return await _proxy_web(request, path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
