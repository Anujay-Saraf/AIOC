from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import os
from typing import Any, Dict, List, Protocol

import httpx

from mock_data import load_deployments, load_logs, load_metrics, load_service_config


class TelemetryProvider(Protocol):
    name: str

    def logs(self, service: str, timestamp: str) -> List[Dict[str, Any]]:
        ...

    def metrics(self, service: str, timestamp: str) -> List[Dict[str, Any]]:
        ...

    def deployments(self, service: str, timestamp: str) -> List[Dict[str, Any]]:
        ...

    def service_config(self) -> Dict[str, Any]:
        ...


@dataclass(frozen=True)
class MockTelemetryProvider:
    name: str = "mock"

    def logs(self, service: str, timestamp: str) -> List[Dict[str, Any]]:
        return load_logs(service, timestamp)

    def metrics(self, service: str, timestamp: str) -> List[Dict[str, Any]]:
        return load_metrics(service, timestamp)

    def deployments(self, service: str, timestamp: str) -> List[Dict[str, Any]]:
        return load_deployments(service, timestamp)

    def service_config(self) -> Dict[str, Any]:
        return load_service_config()


def _replace_service(template: str, service: str) -> str:
    return template.replace("$service", service)


def _iso_window(timestamp: str, minutes: int = 30) -> tuple[str, str]:
    try:
        end = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        end = datetime.now()
    start = end - timedelta(minutes=minutes)
    return start.isoformat(), end.isoformat()


@dataclass(frozen=True)
class OpenSourceTelemetryProvider:
    name: str = "oss"

    def logs(self, service: str, timestamp: str) -> List[Dict[str, Any]]:
        loki_url = os.getenv("LOKI_URL", "").rstrip("/")
        if not loki_url:
            return load_logs(service, timestamp)
        query = _replace_service(os.getenv("LOKI_QUERY", '{service="$service"}'), service)
        start, end = _iso_window(timestamp)
        try:
            with httpx.Client(timeout=8) as client:
                response = client.get(
                    f"{loki_url}/loki/api/v1/query_range",
                    params={"query": query, "start": start, "end": end, "limit": 200},
                )
                response.raise_for_status()
            streams = response.json().get("data", {}).get("result", [])
            logs: List[Dict[str, Any]] = []
            for stream in streams:
                labels = stream.get("stream", {})
                for raw_ts, line in stream.get("values", []):
                    level = labels.get("level") or _infer_level(str(line))
                    logs.append(
                        {
                            "timestamp": raw_ts,
                            "level": level,
                            "service": service,
                            "message": str(line),
                            "source": "loki",
                            "labels": labels,
                        }
                    )
            return logs or load_logs(service, timestamp)
        except Exception as exc:
            print(f"[connectors] Loki fetch failed, using mock logs: {exc}")
            return load_logs(service, timestamp)

    def metrics(self, service: str, timestamp: str) -> List[Dict[str, Any]]:
        prometheus_url = os.getenv("PROMETHEUS_URL", "").rstrip("/")
        if not prometheus_url:
            return load_metrics(service, timestamp)
        query_map = {
            "error_rate": os.getenv("PROMETHEUS_QUERY_ERROR_RATE", 'error_rate{service="$service"}'),
            "latency_ms": os.getenv("PROMETHEUS_QUERY_LATENCY", 'latency_ms{service="$service"}'),
            "cpu_percent": os.getenv("PROMETHEUS_QUERY_CPU", 'cpu_percent{service="$service"}'),
            "memory_mb": os.getenv("PROMETHEUS_QUERY_MEMORY", 'memory_mb{service="$service"}'),
        }
        metrics: List[Dict[str, Any]] = []
        for metric_name, query_template in query_map.items():
            value = self._prometheus_query(prometheus_url, _replace_service(query_template, service))
            if value is not None:
                metrics.append(
                    {
                        "metric_name": metric_name,
                        "value": value,
                        "window": "incident",
                        "source": "prometheus",
                    }
                )
        if metrics:
            baselines = _baseline_from_mock(service, timestamp)
            return baselines + metrics
        return load_metrics(service, timestamp)

    def deployments(self, service: str, timestamp: str) -> List[Dict[str, Any]]:
        gitlab_url = os.getenv("GITLAB_URL", "").rstrip("/")
        project_id = os.getenv("GITLAB_PROJECT_ID", "")
        token = os.getenv("GITLAB_TOKEN", "")
        if gitlab_url and project_id:
            try:
                headers = {"PRIVATE-TOKEN": token} if token else {}
                with httpx.Client(timeout=8, headers=headers) as client:
                    response = client.get(
                        f"{gitlab_url}/api/v4/projects/{project_id}/deployments",
                        params={"environment": service, "order_by": "updated_at", "sort": "desc", "per_page": 10},
                    )
                    response.raise_for_status()
                return [
                    {
                        "timestamp": item.get("updated_at") or item.get("created_at"),
                        "version": str(item.get("deployable", {}).get("commit", {}).get("short_id") or item.get("id")),
                        "changes": [item.get("status", "deployment"), item.get("ref", "")],
                        "source": "gitlab",
                    }
                    for item in response.json()
                ] or load_deployments(service, timestamp)
            except Exception as exc:
                print(f"[connectors] GitLab deployments fetch failed, using mock deployments: {exc}")
        alertmanager_url = os.getenv("ALERTMANAGER_URL", "").rstrip("/")
        if alertmanager_url:
            try:
                with httpx.Client(timeout=8) as client:
                    response = client.get(f"{alertmanager_url}/api/v2/alerts")
                    response.raise_for_status()
                deployments = []
                for item in response.json():
                    labels = item.get("labels", {})
                    annotations = item.get("annotations", {})
                    if labels.get("service") == service and "deploy" in str(labels).lower():
                        deployments.append(
                            {
                                "timestamp": item.get("startsAt"),
                                "version": labels.get("version", labels.get("alertname", "alertmanager")),
                                "changes": [annotations.get("summary", "Alertmanager deployment signal")],
                                "source": "alertmanager",
                            }
                        )
                return deployments or load_deployments(service, timestamp)
            except Exception as exc:
                print(f"[connectors] Alertmanager fetch failed, using mock deployments: {exc}")
        return load_deployments(service, timestamp)

    def service_config(self) -> Dict[str, Any]:
        return load_service_config()

    def _prometheus_query(self, prometheus_url: str, query: str) -> float | None:
        try:
            with httpx.Client(timeout=8) as client:
                response = client.get(f"{prometheus_url}/api/v1/query", params={"query": query})
                response.raise_for_status()
            result = response.json().get("data", {}).get("result", [])
            if not result:
                return None
            return float(result[0]["value"][1])
        except Exception as exc:
            print(f"[connectors] Prometheus query failed for {query}: {exc}")
            return None


def _baseline_from_mock(service: str, timestamp: str) -> List[Dict[str, Any]]:
    return [m for m in load_metrics(service, timestamp) if str(m.get("window", "")).lower() == "baseline"]


def _infer_level(line: str) -> str:
    lowered = line.lower()
    if "critical" in lowered:
        return "CRITICAL"
    if "error" in lowered or "exception" in lowered or "timeout" in lowered:
        return "ERROR"
    if "warn" in lowered:
        return "WARN"
    return "INFO"


def get_telemetry_provider() -> TelemetryProvider:
    if os.getenv("TELEMETRY_PROVIDER", "mock").lower() == "oss":
        return OpenSourceTelemetryProvider()
    return MockTelemetryProvider()
