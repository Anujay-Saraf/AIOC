from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List

from agents import IncidentState


@dataclass(frozen=True)
class CompactContext:
    """A small, LLM-oriented view of the incident with explicit provenance."""

    context: Dict[str, Any]
    included_evidence_ids: List[str]
    manifest: Dict[str, Any]

    def as_json(self) -> str:
        return json.dumps(self.context, ensure_ascii=False, indent=2, default=str)


def build_compact_context(
    state: IncidentState,
    *,
    max_log_anomalies: int = 6,
    max_metric_anomalies: int = 6,
    max_deployments: int = 3,
    max_evidence_per_anomaly: int = 2,
) -> CompactContext:
    included: List[str] = []
    redaction_count: int = 0

    def take(items: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        return list(items[: max(0, int(limit))])

    def redact(value: Any) -> Any:
        nonlocal redaction_count
        if not isinstance(value, str):
            return value
        patterns = [
            r"sk-[A-Za-z0-9_-]+",
            r"(?i)(api[_-]?key|token|secret|password)=\S+",
            r"\b[\w.-]+@[\w.-]+\.\w+\b",
            r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
        ]
        result = value
        for pattern in patterns:
            result, count = re.subn(pattern, "[REDACTED]", result)
            redaction_count += count
        return result

    def anomaly_rank(item: Dict[str, Any]) -> tuple[int, float, int]:
        sev = {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(
            str(item.get("severity", "")).lower(),
            4,
        )
        deviation = abs(float(item.get("percent_change", item.get("spike_factor", 0)) or 0))
        return (sev, -deviation, -int(item.get("count", 1) or 1))

    ranked_logs = sorted(state.log_anomalies, key=anomaly_rank)
    ranked_metrics = sorted(state.metric_anomalies, key=anomaly_rank)

    compact_logs: List[Dict[str, Any]] = []
    for anomaly in take(ranked_logs, max_log_anomalies):
        ev_ids: List[str] = []
        for ref in (anomaly.get("evidence_refs") or [])[:max_evidence_per_anomaly]:
            ev_id = ref.get("evidence_id")
            if ev_id:
                ev_ids.append(ev_id)
        included += ev_ids
        compact_logs.append(
            {
                "type": anomaly.get("type"),
                "severity": anomaly.get("severity"),
                "count": anomaly.get("count"),
                "spike_factor": anomaly.get("spike_factor"),
                "first_seen": anomaly.get("first_seen"),
                "last_seen": anomaly.get("last_seen"),
                "evidence_refs": ev_ids,
                "evidence_samples": [
                    redact(sample)
                    for sample in (anomaly.get("evidence") or [])[:max_evidence_per_anomaly]
                ],
            }
        )

    compact_metrics: List[Dict[str, Any]] = []
    for anomaly in take(ranked_metrics, max_metric_anomalies):
        ev_id = anomaly.get("evidence_id")
        if ev_id:
            included.append(ev_id)
        compact_metrics.append(
            {
                "metric_name": anomaly.get("metric_name"),
                "baseline": anomaly.get("baseline"),
                "current": anomaly.get("current"),
                "percent_change": anomaly.get("percent_change"),
                "severity": anomaly.get("severity"),
                "evidence_id": ev_id,
            }
        )

    compact_deploys: List[Dict[str, Any]] = []
    for dep in take(state.deployment_changes, max_deployments):
        ev_id = dep.get("evidence_id")
        if ev_id:
            included.append(ev_id)
        compact_deploys.append(
            {
                "timestamp": dep.get("timestamp"),
                "version": dep.get("version"),
                "changes": (dep.get("changes") or [])[:3],
                "evidence_id": ev_id,
            }
        )

    ctx: Dict[str, Any] = {
        "incident": {
            "id": state.incident_id,
            "timestamp": state.timestamp,
            "service": state.service,
            "severity": state.severity,
            "alert_description": state.alert_description,
        },
        "log_anomalies": compact_logs,
        "metric_anomalies": compact_metrics,
        "deployment_changes": compact_deploys,
    }
    # Dedup, keep order
    seen: set[str] = set()
    uniq: List[str] = []
    for x in included:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    manifest = {
        "policy_version": "ranked-v1",
        "included_evidence_ids": uniq,
        "excluded_log_anomalies": max(0, len(state.log_anomalies) - len(compact_logs)),
        "excluded_metric_anomalies": max(0, len(state.metric_anomalies) - len(compact_metrics)),
        "excluded_deployments": max(0, len(state.deployment_changes) - len(compact_deploys)),
        "estimated_tokens": max(1, len(json.dumps(ctx, default=str)) // 4),
        "redaction_count": redaction_count,
    }
    return CompactContext(context=ctx, included_evidence_ids=uniq, manifest=manifest)
