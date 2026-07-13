# AIOC Workflow — End-to-End Run-Through

Step-by-step walkthrough of what happens when an incident is triggered through the AI Operations Command Center. For technical internals, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Quick Summary

| Phase | Agents | Status Emitted | Time (heuristic) |
|---|---|---|---|
| 1. Triage | Incident Commander | `alert_triaged` | ~0.5s |
| 2. Investigation | Log Analysis + Metrics Analysis + Deployment Analysis | `logs_analyzed` → `metrics_analyzed` → `deployments_analyzed` | ~1.5s |
| 3. Root Cause | RCA Agent → Debate → (retry loop?) | `rca_completed` | ~1–3s |
| 4. Impact | Business Impact | `impact_calculated` | ~0.3s |
| 5. Recovery | Recovery Recommendations | `recovery_planned` | ~0.5s |
| 6. Summary | Executive Summary | `summary_ready` | ~0.5s |
| 7. HITL Gate | Human Approval (graph pauses) | `needs_human_review` | until human acts |
| 8. Close | Learning & Memory | `complete` | ~0.3s |

---

## Step-by-Step

### Step 1 — Alert Arrives

An alert is sent to `POST /api/incidents/trigger` with a scenario name. FastAPI creates an `IncidentState` record (with a unique `incident_id`), stores it in `incident_store`, and kicks off `_run_analysis` as a background asyncio task. The API returns `202 Accepted` immediately with the `incident_id`.

The graph entry point is `route_next_action`. Since `completed_steps` is empty, the router deterministically selects `incident_commander`.

---

### Step 2 — Incident Commander Agent (AIOC Agent #1)

**File:** `agents/incident_commander.py`  
**Status:** `alert_triaged` | `lifecycle_status: investigating`

What it does:
- Normalizes the alert (severity mapping, service lookup)
- Loads the service profile: ownership team, on-call, SLA tier
- Resolves dependencies and upstream services
- Loads runbooks for this service
- Builds the escalation path (L1 → L2 → L3)
- Determines the rollback plan and blast radius
- Fetches raw telemetry: logs, metrics, deployment changes
- Retrieves knowledge: `build_knowledge_context()` searches SQLite FTS for runbooks and similar past incidents

State written: `service_profile`, `ownership`, `dependencies`, `runbooks`, `escalation_path`, `rollback_plan`, `blast_radius`, `raw_logs`, `raw_metrics`, `deployment_changes`, `retrieval_results`

---

### Step 3 — Log Analysis Agent (AIOC Agent #2)

**File:** `agents/log_analysis.py`  
**Status:** `logs_analyzed`

What it does:
- Scans `raw_logs` for error patterns: timeouts, connection errors, GC pauses, retry storms, OOM signals
- Tags each anomaly with `type`, `count`, `severity`, `first_seen`, `last_seen`
- Builds `log_context_cache` — a structured summary of log evidence for RCA consumption
- Records reasoning in `agent_invocations`

State written: `log_anomalies`, `log_context_cache`

---

### Step 4 — Metrics Analysis Agent (AIOC Agent #3)

**File:** `agents/metrics_analysis.py`  
**Status:** `metrics_analyzed`

What it does:
- Compares incident-window metrics (CPU, memory, latency, error rate, throughput) against established baselines
- Identifies anomalies: spikes, saturations, threshold breaches
- Tags each anomaly with `metric_name`, `current`, `baseline`, `deviation`, `severity`

State written: `metric_anomalies`

---

### Step 5 — Deployment Analysis Agent (AIOC Agent #4) — *New*

**File:** `agents/deployment_analysis.py`  
**Status:** `deployments_analyzed`

What it does:
- Reviews `deployment_changes` from the incident window
- Classifies each deployment by temporal correlation:
  - `likely_trigger` — pushed within 15 minutes of the incident
  - `probable_cause` — within 1 hour
  - `possible_cause` — within 4 hours
  - `historical` — older, context only
- Flags risky change types: pool sizes, connection limits, timeouts, retry configs, config/secret changes, feature flag flips
- Generates structured `deployment_analysis` with:
  - `correlation_summary` — one-sentence summary of deployment impact
  - `overall_risk` — `high`/`medium`/`low` classification based on change types
  - `risk_level` — detailed risk assessment for rollback consideration
  - `recommended_action` — whether to monitor, rollback, or investigate further
- Produces risk tags for each deployment change that feed into HITL approval gating

Why it's separate: Deployment correlation is a **factual** investigation step (what changed, when). Running it before RCA prevents the RCA agent from anchoring its hypothesis on the deployment before weighing log and metric evidence.

**Deployment Data Sources:**
- Change events from CI/CD pipeline (GitHub Actions, GitLab CI, Jenkins)
- Deployment metadata: commit SHAs, author, timestamp, changed files, config diff
- Feature flag state changes via LaunchDarkly/Flagsmith
- Infrastructure changes via Terraform plan diffs

State written: `deployment_analysis` (structured findings, `correlation_summary`, `overall_risk`, `risk_level`, `recommended_action`, `risk_tags`)

---

### Step 6 — Root Cause Analysis Agent (AIOC Agent #5)

**File:** `agents/rca_agent.py` + `agents/rca_analysis.py`  
**Status:** `rca_completed`

What it does:
- Synthesizes all evidence: `log_anomalies` + `metric_anomalies` + `deployment_analysis`
- With LLM: produces a hypothesis with strict JSON schema — must cite evidence literally present in the data, must name 2 ruled-out alternatives with the data point that eliminated each
- Without LLM: heuristic pattern matching (timeout → pool exhaustion, memory + GC → memory leak, etc.)
- Checks `similar_incidents` from memory for "seen this before" callouts
- Produces `rca_confidence` (0.0–1.0)

State written: `root_cause`, `rca_confidence`, `similar_incidents`

---

### Step 6b — RCA Debate/Validation

**File:** `agents/debate.py`  
**Triggered by:** direct edge from `run_rca`

What it does:
- Evidence critic role: checks each piece of supporting evidence is actually in the data
- Debate judge role: scores the overall hypothesis strength
- If `rca_confidence < 0.7` and `analysis_iterations < max_iterations`: marks `rca_analysis` incomplete → `request_more_data` (retry loop)
- If `rca_confidence >= 0.7`: proceeds to `business_impact`

State written: `debate_rounds`, updated `rca_confidence`

---

### Step 6c — Request More Data (retry loop)

**File:** `agents/request_more_data_agent.py`  
**Triggered when:** confidence < 0.7

What it does:
- Widens the log/deployment search window
- Re-fetches additional telemetry
- Removes `rca_analysis` from `completed_steps` → triggers another RCA pass
- Bounded by `max_iterations` — cannot loop forever

---

### Step 7 — Business Impact Agent (AIOC Agent #6)

**File:** `agents/business_impact.py`  
**Status:** `impact_calculated`

What it does:
- Looks up service user base and revenue-per-user from service config
- Derives impact rate from `error_rate` metric anomaly (or service default)
- Calculates `affected_users`, `estimated_revenue_impact_per_minute`, `estimated_cost_impact_per_minute`
- Produces confidence bounds (lower/upper) and `business_risk_level`

State written: `affected_users`, `estimated_revenue_impact_per_minute`, `business_risk_level`, `revenue_impact_justification`

---

### Step 8 — Recovery Recommendation Agent (AIOC Agent #7) — *New*

**File:** `agents/recovery_recommendations.py`  
**Status:** `recovery_planned`

What it does:
- Generates 4–6 ordered, risk-tagged recovery steps
- With LLM: grounded in the specific root cause, deployment correlation, and blast radius
- Without LLM: hypothesis-pattern playbooks (db pool exhaustion, memory leak, cache stampede, retry storm, traffic surge, etc.)
- Flags high-risk steps (`requires_approval: true`): rollback, restart, failover, scale operations
- Determines whether rollback is recommended based on `deployment_analysis` trigger findings
- Produces safety checks and an escalation trigger condition

Why it's separate: Recovery planning is an engineering decision — different from the executive narrative. Separating them allows each to be independently re-generated and approved.

State written: `recovery_recommendations` (flat list), `recovery_plan` (structured with steps, rollback, safety checks)

---

### Step 9 — Executive Summary Agent (AIOC Agent #8)

**File:** `agents/executive_summary.py`  
**Status:** `summary_ready`

What it does:
- Produces `engineering_summary` — technical timeline with anomaly counts and evidence
- Produces `executive_summary` — plain-language narrative for leadership (≤120 words, leads with impact)
- With LLM: rewrites executive summary grounded in RCA + impact + deployment correlation
- Without LLM: structured template using all prior findings

State written: `engineering_summary`, `executive_summary`

---

### Step 10 — Human Approval Gate (HITL)

**Graph behaviour:** compiled with `interrupt_before=["human_approval"]` + SQLite checkpointer

What happens:
1. Before `human_approval` node runs, LangGraph **pauses and persists** the full graph state to `data/checkpoints.sqlite3` (keyed by `incident_id`)
2. Incident `lifecycle_status` is set to `needs_human_review`
3. UI shows the incident with all recovery steps and Approve/Reject buttons
4. Engineer reviews `recovery_plan.steps` — each high-risk step shows `requires_approval: true`
5. Engineer calls `POST /api/incidents/{id}/remediation/{step}/decision` for each decision
6. Once decisions are recorded, `POST /api/incidents/{id}/resume` is called
7. LangGraph resumes from the checkpoint — the `human_approval` node runs, records the decision audit trail, and continues

This is a genuine graph-level pause/resume — not a flag-in-state simulation.

---

### Step 11 — Learning & Memory

**File:** `agents/memory.py` (via `_learning_node`)  
**Status:** `complete` | `lifecycle_status: resolved`

What happens:
- Incident is persisted to `data/incident_memory.json` for future "seen this before" matching
- Post-incident reflection is logged (confidence quality, coverage gaps)
- If `WAR_ROOM_WEBHOOK_URL` is set, a Slack/Discord notification is sent

---

## What the UI Shows (Live)

The frontend polls `GET /api/incidents/{id}` every second. At each poll, it renders:

| UI Section | Data Source |
|---|---|
| Status banner | `current_status` cycling through all 8 phases |
| Agent Reasoning & Audit Trail | `agent_invocations` list (append-only, every agent writes here) |
| Root Cause card | `root_cause.hypothesis`, `rca_confidence`, `ruling_out` |
| Deployment Correlation | `deployment_analysis.correlation_summary`, `overall_risk` |
| Business Impact card | `affected_users`, `estimated_revenue_impact_per_minute`, `business_risk_level` |
| Recovery Actions | `recovery_plan.steps` with risk badges and Approve/Reject buttons |
| Executive Summary | `executive_summary` (plain text, leadership-ready) |
| Seen Before | `similar_incidents` |
| Postmortem export | `GET /api/incidents/{id}/postmortem` — markdown with full timeline + decisions |

---

## Demo Script (5-Minute Scenario)

1. Open dashboard → select **Database Pool Exhaustion** scenario → click **Trigger Incident**
2. Watch the status banner cycle through all 8 agent phases in real time
3. Expand **Agent Reasoning & Audit Trail** — see each agent's reasoning, evidence, and confidence
4. Review the **Deployment Correlation** card — see deployment flagged as `likely_trigger`
5. Review the **Recovery Plan** — note rollback is `recommended: true` with high-risk flag
6. Click **Approve** on rollback action → incident moves to `complete`
7. Download the postmortem — see the full timeline including the approval decision and timestamp
