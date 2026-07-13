---
incident_id: aba9d634-ba49-4169-abf2-9142df64e3d4
title: Incident Postmortem â€” search-api
service: search-api
severity: critical
date: 2026-07-07T19:09:00Z
tags: [incident, postmortem, search-api]
---

# Incident Postmortem â€” search-api

- **Incident ID:** aba9d634-ba49-4169-abf2-9142df64e3d4
- **Trace ID:** aba9d634-ba49-4169-abf2-9142df64e3d4
- **Lifecycle:** resolved
- **Date:** 2026-07-07T19:09:00Z
- **Severity:** critical
- **Alert:** Latency spike from cache stampede and retry amplification

## Executive Summary

We are currently experiencing a critical latency spike in our search API service, impacting approximately 6,840 users. This issue is causing an estimated revenue loss of $171 per minute due to a cache stampede that has led to a retry storm. There have been no recent deployment changes that can be correlated with this problem. To address the situation, we are implementing recovery steps that include re-enabling cache coalescing, pre-warming the cache for critical keys prior to traffic increases, and rolling back a recent change that disabled cache coalescing. We are committed to resolving this issue swiftly to restore normal operations.

## Root Cause

**Cache stampede leading to retry storm amplification** (confidence: 83%)

### Supporting Evidence

- Retry storm detected after cache miss amplification
- Traffic surge pushed cache miss rate above threshold
- Spend spike detected as repeated retries compound request load
- Current error rate: 0.19 (baseline: 0.001)
- Current latency: 1400ms (baseline: 45ms)

### Evidence References

- `log:search-api:1:9bb8e0ff04` (log): Retry storm detected after cache miss amplification
- `log:search-api:4:784efe0164` (log): Retry storm detected after cache miss amplification
- `log:search-api:2:7bec564028` (log): Traffic surge pushed cache miss rate above threshold
- `log:search-api:3:2ddf575c3d` (log): Spend spike detected as repeated retries compound request load
- `metric:search-api:incident:error_rate:7` (metric): Current error rate: 0.19 (baseline: 0.001)
- `metric:search-api:incident:error_rate:7` (metric): Current latency: 1400ms (baseline: 45ms)
- `metric:search-api:incident:error_rate:7` (metric): Metric anomaly: latency_ms
- `metric:search-api:incident:error_rate:7` (metric): Metric anomaly: error_rate

### Confidence Breakdown

- evidence_strength: 1.0
- signal_count: 7
- deploy_correlation: 0.0
- signal_diversity: 1.0
- anomaly_severity: 1.0
- data_completeness: 1.0
- alternatives_ruled_out: 1.0
- historical_similarity: 0.0
- llm_self_report: 0.9

### Alternatives Considered & Ruled Out

- ~~Hardware failure impacting service~~ â€” No hardware-related logs or anomalies were reported in the context.
- ~~Network issues impacting API responses~~ â€” Latency spikes were attributed to retries and cache misses, not network metrics.

## Business Impact

- Affected users: 6,840
- Estimated revenue impact: $171.00/minute
- Estimated cost impact: $120.00/minute
- Business risk level: unknown

### Impact Justification

- Affected users: 6,840
- Revenue per user per minute: $0.03
- Range: $136.80-$205.20/minute

## Log Context

- Logs scanned: 5
- Error contexts cached: 4

## Recovery Actions

1. Enable cache coalescing (request collapsing) to prevent thundering herd â€” _pending review_
2. Pre-warm cache for hot keys before traffic ramp-up â€” _pending review_
3. Roll back the deployment that disabled cache coalescing â€” _pending review_
4. Add cache hit-rate monitoring and alert on sudden drops â€” _pending review_
5. Tune TTL values to stagger expiry windows â€” _pending review_

## Related Past Incidents

- Incident c8b001c5-7489-445a-8433-177628d6259c on search-api (2026-07-12): Cache miss due to traffic surge causing retry storm â€” same service with overlapping anomaly signature
- Incident 544d2bdc-a525-4edc-a75d-91f0525f67cf on search-api (2026-07-11): Traffic surge caused cache miss leading to retry storm and latency spike â€” same service with overlapping anomaly signature
- Incident 2a944a8f-4f55-4a0c-81b2-4d8d25abe9eb on search-api (2026-07-11): Cache stampede and retry amplification due to traffic surge â€” same service with overlapping anomaly signature

## Investigation Timeline

- `22:55:46` **router_agent** â€” Single valid next step — no LLM decision needed
- `22:55:46` **incident_commander** â€” load_incident_data
- `22:55:46` **knowledge_retrieval** â€” Retrieved operational runbooks and similar incident patterns for grounded investigation.
- `22:55:46` **router_agent** â€” Single valid next step — no LLM decision needed
- `22:55:46` **log_analysis** â€” Scanned 5 log entries; found 0 timeout errors, 0 connection/pool errors, 0 GC warnings, 2 retry/throttle signals, 1 cost signals -> 3 anomaly pattern(s)
- `22:55:46` **router_agent** â€” Single valid next step — no LLM decision needed
- `22:55:46` **metrics_analysis** â€” Compared 4 metrics against baseline; 4 spiked beyond the 50% threshold: traffic_qps +433%, cost_per_minute +500%, latency_ms +3011%, error_rate +18900%
- `22:55:46` **router_agent** â€” Single valid next step — no LLM decision needed
- `22:55:46` **deployment_analysis_agent** â€” No deployment changes recorded for this incident.
- `22:55:46` **router_agent** â€” Single valid next step — no LLM decision needed
- `22:55:50` **memory** â€” This matches incident #30 on search-api from 2026-07-12 — same pattern: Cache miss due to traffic surge causing retry storm (same service with overlapping anomaly signature)
- `22:55:50` **rca_agent** â€” The evidence indicates that a spike in traffic resulted in a high cache miss rate, which subsequently triggered a retry storm, leading to critical latency and error rate increases.
- `22:55:50` **evidence_critic** â€” Evidence coverage and alternative elimination are sufficient
- `22:55:50` **operations_critic** â€” Deployment correlation was explicitly considered; The proposed failure mode matches the observed operational signals
- `22:55:50` **debate_judge** â€” Accepted the RCA because both critics found adequate grounding and operational safety.
- `22:55:50` **router_agent** â€” Single valid next step — no LLM decision needed
- `22:55:50` **business_impact** â€” 6,840 affected users = 36,000 users * 19.0% bounded impact rate from current error_rate metric; revenue impact = 6,840 * $0.03/user/min
- `22:55:50` **router_agent** â€” Single valid next step — no LLM decision needed
- `22:55:51` **recovery_recommendation_agent** â€” Generated 5 recovery steps from 'Cache stampede leading to retry storm amplification' root cause. Rollback not recommended. 2 step(s) require human approval.
- `22:55:51` **router_agent** â€” Single valid next step — no LLM decision needed
- `22:55:51` **executive_summary** â€” generate_summaries
- `22:55:54` **executive_summary** â€” llm_enhance_summary
- `22:55:54` **router_agent** â€” Single valid next step — no LLM decision needed
- `22:55:54` **human_approval_gate** â€” Evaluated recovery actions for blast radius and marked high-risk steps for human approval.
- `22:55:54` **router_agent** â€” Single valid next step — no LLM decision needed
- `22:55:54` **learning_agent** â€” Investigation achieved high-confidence root cause with cited evidence.

---

_Generated automatically by AI Operations Command Center_
