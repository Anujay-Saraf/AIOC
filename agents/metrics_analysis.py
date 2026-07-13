from __future__ import annotations

from typing import Any

from agents import IncidentState
from agents.telemetry import Timer, record_invocation


def metrics_analysis(state: IncidentState) -> IncidentState:
    t = Timer.begin()
    baseline_metrics: dict[str, float] = {}
    incident_metrics: dict[str, float] = {}

    for metric in state.raw_metrics:
        metric_name: str = metric.get("metric_name", "").lower()
        value: float = float(metric.get("value", 0))
        window: str = metric.get("window", "").lower()

        if window == "baseline":
            baseline_metrics[metric_name] = value
        elif window == "incident":
            incident_metrics[metric_name] = value

    if not baseline_metrics:
        baseline_metrics = {
            "cpu_percent": 25.0,
            "memory_mb": 500.0,
            "latency_ms": 50.0,
            "error_rate": 0.001,
            "traffic_qps": 1000.0,
            "cost_per_minute": 25.0,
        }

    for metric_name, baseline_value in baseline_metrics.items():
        if metric_name in incident_metrics:
            current_value: float = incident_metrics[metric_name]
            percent_change: float = ((current_value - baseline_value) / baseline_value * 100) if baseline_value > 0 else 0

            if abs(percent_change) > 50:
                anomaly: dict[str, Any] = {
                    "metric_name": metric_name,
                    "baseline": baseline_value,
                    "current": current_value,
                    "percent_change": round(percent_change, 2),
                    "severity": "critical"
                    if abs(percent_change) > 150
                    else "high"
                    if abs(percent_change) > 75
                    else "medium",
                    "evidence_id": metric.get("evidence_id"),
                }
                state.metric_anomalies.append(anomaly)

    record_invocation(
        state,
        agent="metrics_analysis",
        action="analyze_metrics",
        source="heuristic",
        reasoning=(
            f"Compared {len(baseline_metrics)} metrics against baseline; "
            f"{len(state.metric_anomalies)} spiked beyond the 50% threshold: "
            + (", ".join(
                f"{m['metric_name']} {m['percent_change']:+.0f}%"
                for m in state.metric_anomalies
            ) or "none")
        ),
        findings={
            "anomalies_detected": len(state.metric_anomalies),
            "metrics_checked": len(baseline_metrics),
        },
        output_refs={
            "metric_anomaly_names": [m.get("metric_name") for m in state.metric_anomalies],
            "evidence_ids": [
                m.get("evidence_id")
                for m in state.metric_anomalies
                if m.get("evidence_id")
            ][:25],
        },
        latency_ms=t.ms(),
    )

    return state
